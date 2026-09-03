"""
Personal expense-tracking Telegram bot.

Commands:
  /start                  intro + help
  /help                   command reference
  /settarget 100          set today's (and future) daily spending target
  /log 12.50 lunch        log a personal expense against today's allowance
  /log 20 USD taxi        same, but in a specific currency (converted to your base currency)
  /claim 300 groceries    log a claimable/reimbursable expense (separate pool)
  /claimed                mark all pending claimables as reimbursed, clears them
  /balance                target, balance, spent today, pending claimables, streak
  /summary [today|week|month]   AI-written spending breakdown by category
  /recent [n]             last n logged expenses with their IDs (default 10)
  /undo                   remove the single most recent expense
  /edit <id> <amount> [description...]   fix a mislogged expense
  /delete <id>            remove a specific expense by ID

You can also just type naturally, e.g. "spent 15 on uber" or
"paid 20 USD for taxi, claimable" and the bot will parse, categorize, and
log it -- asking a quick follow-up question only when something's unclear.

Run with: python bot.py
"""

import logging
from datetime import date, timedelta

from dotenv import load_dotenv
load_dotenv()  # must run before `import config`, which reads env vars at import time

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
import db
import fx
import ai
import trends

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# ---------- access control ----------

def _allowed(update: Update) -> bool:
    if not config.ALLOWED_CHAT_IDS:
        return True
    return update.effective_chat.id in config.ALLOWED_CHAT_IDS


async def _reject_if_not_allowed(update: Update) -> bool:
    if not _allowed(update):
        await update.message.reply_text("This bot is private. Ask the owner to add your chat ID.")
        return True
    return False


# ---------- formatting helpers ----------

def _money(x: float, currency: str = None) -> str:
    currency = currency or config.BASE_CURRENCY
    sign = "-" if x < 0 else ""
    return f"{sign}{currency} {abs(x):,.2f}"


def _status_text(status: dict) -> str:
    lines = [
        f"Today's target: {_money(status['daily_target'])}",
        f"Rolled-over balance: {_money(status['balance'])}",
        f"Spent today: {_money(status['spent_today'])}",
        f"Available today: {_money(status['available_today'])}",
    ]
    if status["pending_claimable"] > 0:
        lines.append(f"Pending claimables: {_money(status['pending_claimable'])}")
    if status["current_streak"] > 0:
        lines.append(f"Streak: {status['current_streak']} day(s) within budget (best: {status['best_streak']})")
    return "\n".join(lines)


def _expense_line(row: dict) -> str:
    claim_tag = " [claimable]" if row.get("is_claimable") else ""
    claimed_tag = " (claimed)" if row.get("is_claimed") else ""
    return (f"#{row['id']} {_money(row['amount'], row.get('currency'))} -- "
            f"{row['description']} [{row['category']}]{claim_tag}{claimed_tag} ({row['expense_date']})")


def _calorie_range(row: dict) -> str:
    low, high, est = row.get("calories_low"), row.get("calories_high"), row.get("calories_estimate")
    if est is None:
        return "unknown kcal"
    if low is not None and high is not None and (low, high) != (est, est):
        return f"~{low:.0f}-{high:.0f} kcal (central ~{est:.0f})"
    return f"~{est:.0f} kcal"


def _meal_line(row: dict) -> str:
    items = ", ".join(row.get("items") or []) or "unspecified"
    type_tag = f" [{row['meal_type']}]" if row.get("meal_type") else ""
    water_tag = f", {row['water_ml']:.0f}ml water" if row.get("water_ml") else ""
    return f"#{row['id']} {items}{type_tag} -- {_calorie_range(row)}{water_tag} ({row['meal_date']})"


def _workout_line(row: dict) -> str:
    bits = []
    if row.get("duration_min"):
        bits.append(f"{row['duration_min']:.0f} min")
    if row.get("distance_km"):
        bits.append(f"{row['distance_km']:.1f} km")
    detail = f" ({', '.join(bits)})" if bits else ""
    notes = f" -- {row['notes']}" if row.get("notes") else ""
    return f"#{row['id']} {row.get('activity') or 'workout'}{detail}{notes} ({row['workout_date']})"


def _vitals_line(row: dict) -> str:
    bits = []
    if row.get("weight_kg") is not None:
        bits.append(f"{row['weight_kg']:.1f}kg")
    if row.get("sleep_hours") is not None:
        bits.append(f"{row['sleep_hours']:.1f}h sleep")
    if row.get("knee_pain") is not None:
        bits.append(f"knee {row['knee_pain']:.0f}/10")
    summary = ", ".join(bits) or "check-in"
    notes = f" -- {row['notes']}" if row.get("notes") else ""
    return f"#{row['id']} {summary}{notes} ({row['vitals_date']})"


def _daily_meal_totals_text(chat_id: int) -> str:
    totals = db.get_daily_meal_totals(chat_id, db.today_str())
    line = f"Today's running total: ~{totals['calories']:.0f} kcal"
    if totals["water_ml"]:
        line += f", {totals['water_ml']:.0f}ml water"
    return line


async def _send_alert_if_needed(update: Update, chat_id: int):
    if db.maybe_alert(chat_id):
        status = db.get_status(chat_id)
        pct = int(config.BUDGET_ALERT_THRESHOLD * 100)
        await update.message.reply_text(
            f"Heads up: you've crossed {pct}% of today's available budget.\n\n{_status_text(status)}"
        )


# ---------- commands ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    db.get_or_create_user(update.effective_chat.id)
    await update.message.reply_text(
        "Hey! I'll track your daily spending allowance, claimable expenses, meals, workouts, and vitals.\n\n"
        "Set a daily target: /settarget 100\n"
        "Log a personal expense: /log 12.50 lunch\n"
        "Log in another currency: /log 20 USD taxi\n"
        "Log a claimable one: /claim 300 groceries\n"
        "Cleared a reimbursement: /claimed\n"
        "Check where you stand: /balance\n"
        "Get a breakdown: /summary week\n"
        "See recent entries: /recent\n"
        "Fix a mistake: /undo, /edit <id> <amount>, or /delete <id>\n\n"
        "Log a meal: /logmeal chicken rice and iced tea, or just send a photo of your food\n"
        "See recent meals: /recentmeals\n"
        "Log a workout: /logworkout tennis for an hour\n"
        "See recent workouts: /recentworkouts\n"
        "Log vitals: /logvitals weight 76.6, slept 5.5 hours, knee 2/10\n"
        "See recent check-ins: /recentvitals\n\n"
        "Or just tell me naturally, e.g. \"spent 15 on uber\", \"had a mango\", \"played tennis for an hour\", "
        "\"weight 76.6, slept 5.5 hours\"."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def settarget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /settarget 100")
        return
    try:
        amount = float(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a number. Try: /settarget 100")
        return
    db.set_daily_target(chat_id, amount)
    status = db.get_status(chat_id)
    await update.message.reply_text(
        f"Daily target set to {_money(amount)}.\n\n{_status_text(status)}"
    )


def _split_amount_currency_description(args):
    """Parses ['20', 'USD', 'taxi', 'ride'] -> (20.0, 'USD', 'taxi ride')
    or ['12.50', 'lunch'] -> (12.5, None, 'lunch')."""
    if not args:
        return None, None, ""
    try:
        amount = float(args[0])
    except ValueError:
        return None, None, ""
    rest = args[1:]
    currency = None
    if rest:
        maybe_currency = fx.normalize_currency(rest[0])
        if maybe_currency:
            currency = maybe_currency
            rest = rest[1:]
    description = " ".join(rest)
    return amount, currency, description


async def log_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    await _handle_explicit_log(update, context, is_claimable=False)


async def claim_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    await _handle_explicit_log(update, context, is_claimable=True)


async def _handle_explicit_log(update: Update, context: ContextTypes.DEFAULT_TYPE, is_claimable: bool):
    chat_id = update.effective_chat.id
    cmd = "/claim" if is_claimable else "/log"
    amount, currency, description = _split_amount_currency_description(context.args)
    if amount is None:
        await update.message.reply_text(f"Usage: {cmd} 12.50 [CURRENCY] [description]")
        return
    description = description or ("claimable expense" if is_claimable else "expense")
    category = ai.categorize(description)
    db.add_expense(chat_id, amount, currency, description, category, is_claimable=is_claimable)

    if is_claimable:
        status = db.get_status(chat_id)
        await update.message.reply_text(
            f"Logged claimable: {_money(amount, currency)} -- {description} [{category}]\n"
            f"Pending claimables: {_money(status['pending_claimable'])}\n"
            "(This doesn't touch your daily allowance.)"
        )
    else:
        status = db.get_status(chat_id)
        await update.message.reply_text(
            f"Logged: {_money(amount, currency)} -- {description} [{category}]\n\n{_status_text(status)}"
        )
        await _send_alert_if_needed(update, chat_id)


async def claimed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    count, total = db.clear_claimables(chat_id)
    if count == 0:
        await update.message.reply_text("No pending claimables to clear.")
    else:
        await update.message.reply_text(
            f"Cleared {count} claimable expense(s) totaling {_money(total)}. Nice."
        )


def _month_to_date_text(chat_id: int) -> str:
    mtd = db.get_month_to_date_total(chat_id)
    return f"Month to date ({mtd['days_elapsed']} day(s)): {_money(mtd['total'])} spent since {mtd['month_start']}"


def _balance_text(chat_id: int) -> str:
    """Shared by the /balance command and the natural-language 'show me my
    balance' intent, so both paths are guaranteed to say the same thing --
    a free-text balance query is not a second, separately-maintained
    implementation."""
    status = db.get_status(chat_id)
    text = _status_text(status) + "\n\n" + _month_to_date_text(chat_id)
    pending = db.get_pending_claimables(chat_id)
    if pending:
        lines = [f"  #{p['id']} {_money(p['amount'], p['currency'])} -- {p['description']} [{p['category']}]"
                  for p in pending]
        text += "\n\nPending claimables:\n" + "\n".join(lines)
    return text


def _recent_text(chat_id: int, limit: int = 10) -> str:
    """Shared by /recent and the natural-language 'show me my recent
    expenses' intent -- see _balance_text's docstring for why."""
    rows = db.get_recent_expenses(chat_id, limit=limit)
    if not rows:
        return "No expenses logged yet."
    lines = [_expense_line(r) for r in rows]
    return "Recent expenses:\n" + "\n".join(lines)


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    await update.message.reply_text(_balance_text(update.effective_chat.id))


async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    limit = 10
    if context.args:
        try:
            limit = max(1, min(50, int(context.args[0])))
        except ValueError:
            pass
    await update.message.reply_text(_recent_text(chat_id, limit=limit))


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    row = db.delete_most_recent(chat_id)
    if row is None:
        await update.message.reply_text("Nothing to undo.")
        return
    status = db.get_status(chat_id)
    await update.message.reply_text(
        f"Removed: {_money(row['amount'], row['currency'])} -- {row['description']} [{row['category']}]\n\n"
        f"{_status_text(status)}"
    )


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /delete <id> (see /recent for IDs)")
        return
    try:
        expense_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /delete <id> (see /recent for IDs)")
        return
    row = db.delete_expense(chat_id, expense_id)
    if row is None:
        await update.message.reply_text(f"No expense #{expense_id} found.")
        return
    status = db.get_status(chat_id)
    await update.message.reply_text(
        f"Deleted: {_money(row['amount'], row['currency'])} -- {row['description']} [{row['category']}]\n\n"
        f"{_status_text(status)}"
    )


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /edit <id> <new amount> [new description...] (see /recent for IDs)")
        return
    try:
        expense_id = int(context.args[0])
        new_amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Usage: /edit <id> <new amount> [new description...]")
        return
    new_description = " ".join(context.args[2:]) or None
    new_category = ai.categorize(new_description) if new_description else None

    row = db.edit_expense(chat_id, expense_id, new_amount=new_amount,
                           new_description=new_description, new_category=new_category)
    if row is None:
        await update.message.reply_text(f"No expense #{expense_id} found.")
        return
    status = db.get_status(chat_id)
    await update.message.reply_text(
        f"Updated #{expense_id}: {_money(row['amount'], row['currency'])} -- "
        f"{row['description']} [{row['category']}]\n\n{_status_text(status)}"
    )


# ---------- meals ----------

async def _log_meal_and_reply(update: Update, chat_id: int, data: dict):
    """Shared by /logmeal, photo logging, and the natural-language log_meal
    intent -- one insert, one reply shape, so all three paths are
    guaranteed to say the same thing (see _balance_text's docstring for the
    same reasoning applied to /balance)."""
    meal_id = db.add_meal(
        chat_id, data.get("meal_type"), data.get("items") or data.get("meal_items"),
        data.get("calories_low"), data.get("calories_high"), data.get("calories_estimate"),
        water_ml=data.get("water_ml"),
    )
    row = db.get_meal(chat_id, meal_id)
    items = ", ".join(row["items"]) or "meal"
    water_line = f"\nWater: +{row['water_ml']:.0f}ml" if row.get("water_ml") else ""
    await update.message.reply_text(
        f"Logged: {items} -- {_calorie_range(row)}{water_line}\n\n{_daily_meal_totals_text(chat_id)}"
    )


async def logmeal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    description = " ".join(context.args)
    if not description:
        await update.message.reply_text("Usage: /logmeal chicken rice and an iced tea")
        return
    data = ai.extract_meal(description)
    await _log_meal_and_reply(update, chat_id, data)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A food photo, sent with or without a caption, logs a meal directly --
    no command needed. Any other use for a photo isn't supported yet, so
    this assumes every photo sent to the bot is a meal."""
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    caption = (update.message.caption or "").strip() or None
    photo = update.message.photo[-1]  # highest-resolution size Telegram offers
    tg_file = await photo.get_file()
    image_bytes = bytes(await tg_file.download_as_bytearray())
    data = ai.extract_meal_from_image(image_bytes, caption)
    await _log_meal_and_reply(update, chat_id, data)


async def recentmeals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    limit = 10
    if context.args:
        try:
            limit = max(1, min(50, int(context.args[0])))
        except ValueError:
            pass
    rows = db.get_recent_meals(chat_id, limit=limit)
    if not rows:
        await update.message.reply_text("No meals logged yet.")
        return
    lines = [_meal_line(r) for r in rows]
    await update.message.reply_text(
        "Recent meals:\n" + "\n".join(lines) + f"\n\n{_daily_meal_totals_text(chat_id)}"
    )


# ---------- workouts ----------

async def logworkout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    description = " ".join(context.args)
    if not description:
        await update.message.reply_text("Usage: /logworkout tennis for an hour")
        return
    data = ai.extract_workout(description)
    workout_id = db.add_workout(chat_id, data.get("activity"), data.get("duration_min"),
                                 data.get("distance_km"), data.get("notes"))
    row = db.get_workout(chat_id, workout_id)
    await update.message.reply_text(f"Logged: {_workout_line(row)}")


async def recentworkouts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    limit = 10
    if context.args:
        try:
            limit = max(1, min(50, int(context.args[0])))
        except ValueError:
            pass
    rows = db.get_recent_workouts(chat_id, limit=limit)
    if not rows:
        await update.message.reply_text("No workouts logged yet.")
        return
    await update.message.reply_text("Recent workouts:\n" + "\n".join(_workout_line(r) for r in rows))


# ---------- vitals ----------

async def _log_vitals_and_reply(update: Update, chat_id: int, data: dict):
    vitals_id = db.add_vitals(chat_id, data.get("weight_kg"), data.get("sleep_hours"),
                               data.get("knee_pain"), data.get("notes") or data.get("vitals_notes"))
    row = db.get_vitals(chat_id, vitals_id)
    await update.message.reply_text(f"Logged: {_vitals_line(row)}")


async def logvitals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    description = " ".join(context.args)
    if not description:
        await update.message.reply_text("Usage: /logvitals weight 76.6, slept 5.5 hours, knee 2/10")
        return
    data = ai.extract_vitals(description)
    await _log_vitals_and_reply(update, chat_id, data)


async def recentvitals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    limit = 10
    if context.args:
        try:
            limit = max(1, min(50, int(context.args[0])))
        except ValueError:
            pass
    rows = db.get_recent_vitals(chat_id, limit=limit)
    if not rows:
        await update.message.reply_text("No check-ins logged yet.")
        return
    await update.message.reply_text("Recent check-ins:\n" + "\n".join(_vitals_line(r) for r in rows))


WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    period_arg = context.args[0] if context.args else "week"
    bounds = trends.period_bounds(period_arg, date.today())
    period = bounds["period"]
    length = bounds["length"]
    today = bounds["today"]
    tomorrow = bounds["tomorrow"]
    current_start = bounds["current_start"]
    current_end = bounds["current_end"]
    prev_start = bounds["prev_start"]
    prev_end = bounds["prev_end"]

    current_totals = db.get_category_totals(chat_id, current_start.isoformat(), current_end.isoformat())
    if not current_totals:
        await update.message.reply_text(f"No spending logged in the last {period}.")
        return
    previous_totals = db.get_category_totals(chat_id, prev_start.isoformat(), prev_end.isoformat())

    current_total = sum(r["total"] for r in current_totals)
    previous_total = sum(r["total"] for r in previous_totals)

    user = db.get_or_create_user(chat_id)
    payload = {
        "base_currency": config.BASE_CURRENCY,
        "current_period": {"days": length, "total": round(current_total, 2), "by_category": current_totals},
        "previous_period": {"days": length, "total": round(previous_total, 2), "by_category": previous_totals},
        "streak": {"current": user["current_streak"], "best": user["best_streak"]},
        "month_to_date": db.get_month_to_date_total(chat_id),
    }

    # Day-of-week pattern + budget adherence only make sense with more than a single day of history.
    if period != "today":
        lookback_start = today - timedelta(days=56)  # 8 weeks, for a stable weekday average
        daily = db.get_daily_totals(chat_id, lookback_start.isoformat(), tomorrow.isoformat())
        dow_totals = [0.0] * 7
        dow_counts = [0] * 7
        d = lookback_start
        while d < tomorrow:
            dow = d.weekday()
            dow_totals[dow] += daily.get(d.isoformat(), 0.0)
            dow_counts[dow] += 1
            d += timedelta(days=1)
        payload["avg_spend_by_weekday"] = {
            WEEKDAY_NAMES[i]: round(dow_totals[i] / dow_counts[i], 2)
            for i in range(7) if dow_counts[i] > 0
        }

        period_daily = db.get_daily_totals(chat_id, current_start.isoformat(), current_end.isoformat())
        days_under = sum(
            1 for i in range(length)
            if period_daily.get((current_start + timedelta(days=i)).isoformat(), 0.0) <= user["daily_target"]
        )
        payload["budget_adherence"] = {
            "days_under_target": days_under,
            "days_in_period": length,
            "daily_target": user["daily_target"],
        }

    try:
        text = ai.answer_with_trends(period, payload)
    except Exception:
        logger.exception("AI trend summary failed, falling back to raw breakdown")
        lines = [f"{r['category']}: {_money(r['total'])} ({r['n']}x)" for r in current_totals]
        trend_line = ""
        if previous_total:
            pct = (current_total - previous_total) / previous_total * 100
            direction = "Up" if pct > 0 else "Down"
            trend_line = f"\n{direction} {abs(pct):.0f}% vs the prior {period}."
        text = (f"Spending -- last {period}:\n" + "\n".join(lines) +
                f"\n\nTotal: {_money(current_total)}{trend_line}")
    await update.message.reply_text(text)


# ---------- natural language handling ----------

PENDING_KEY = "pending_expense"
LAST_CORRECTION_KEY = "last_correction"
RECENT_EXPENSES_FOR_AI = 8  # how much history the model gets to resolve "that", "the duplicate", etc.

# A short reply matching one of these, sent as the very next message after a
# correction, reverts it directly -- deterministic and exact-match only (not
# a substring check), so an expense description that happens to contain the
# word "wrong" can't accidentally trigger it.
CORRECTION_UNDO_PHRASES = {
    "undo", "undo that", "undo it", "revert", "no", "nope",
    "wrong", "that's wrong", "thats wrong", "no that's wrong",
}

CORRECTION_ACTIONS = {
    "edit_date", "edit_currency", "edit_amount", "edit_description", "edit_category", "delete",
}


def _recent_for_ai(chat_id: int) -> list:
    rows = db.get_recent_expenses(chat_id, limit=RECENT_EXPENSES_FOR_AI)
    return [
        {
            "id": r["id"], "amount": r["amount"], "currency": r["currency"],
            "description": r["description"], "category": r["category"],
            "expense_date": r["expense_date"], "is_claimable": bool(r["is_claimable"]),
        }
        for r in rows
    ]


def _recent_meals_for_ai(chat_id: int) -> list:
    rows = db.get_recent_meals(chat_id, limit=RECENT_EXPENSES_FOR_AI)
    return [
        {"id": r["id"], "items": r["items"], "meal_type": r["meal_type"],
         "calories_estimate": r["calories_estimate"], "meal_date": r["meal_date"]}
        for r in rows
    ]


def _recent_workouts_for_ai(chat_id: int) -> list:
    rows = db.get_recent_workouts(chat_id, limit=RECENT_EXPENSES_FOR_AI)
    return [
        {"id": r["id"], "activity": r["activity"], "duration_min": r["duration_min"],
         "workout_date": r["workout_date"]}
        for r in rows
    ]


def _recent_vitals_for_ai(chat_id: int) -> list:
    rows = db.get_recent_vitals(chat_id, limit=RECENT_EXPENSES_FOR_AI)
    return [
        {"id": r["id"], "weight_kg": r["weight_kg"], "sleep_hours": r["sleep_hours"],
         "knee_pain": r["knee_pain"], "vitals_date": r["vitals_date"]}
        for r in rows
    ]


async def _revert_last_correction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if there was something to revert (and replies about it),
    False if there was no pending correction snapshot at all."""
    snap = context.chat_data.pop(LAST_CORRECTION_KEY, None)
    if not snap:
        return False
    chat_id = update.effective_chat.id
    action = snap["action"]
    domain = snap.get("domain", "expense")

    if domain in _DOMAIN_OPS:
        # meal/workout/vitals all share the same narrow revert shape -- see
        # _handle_simple_domain_correction for why their correction surface
        # is smaller than expenses'.
        ops = _DOMAIN_OPS[domain]
        if action == "delete":
            restored = ops["restore"](chat_id, snap["row"])
            await update.message.reply_text(f"Restored: {ops['line'](restored)}")
        elif action == "edit_date":
            row = ops["edit_date"](chat_id, snap["expense_id"], snap["old_date"])
            await update.message.reply_text(
                f"Reverted -- {ops['noun']} is back to {row[ops['date_field']]}."
            )
        return True

    if action == "delete":
        restored = db.restore_deleted_expense(chat_id, snap["row"])
        await update.message.reply_text(
            f"Restored: {_money(restored['amount'], restored['currency'])} -- {restored['description']} "
            f"[{restored['category']}] ({restored['expense_date']})"
        )
    elif action == "edit_date":
        row = db.edit_expense_date(chat_id, snap["expense_id"], snap["old_date"])
        await update.message.reply_text(f"Reverted -- date is back to {row['expense_date']}.")
    elif action == "edit_currency":
        row = db.edit_expense(chat_id, snap["expense_id"], new_currency=snap["old_currency"])
        await update.message.reply_text(
            f"Reverted -- currency is back to {_money(row['amount'], row['currency'])}."
        )
    elif action == "edit_amount":
        row = db.edit_expense(chat_id, snap["expense_id"], new_amount=snap["old_amount"])
        await update.message.reply_text(
            f"Reverted -- amount is back to {_money(row['amount'], row['currency'])}."
        )
    elif action == "edit_description":
        row = db.edit_expense(chat_id, snap["expense_id"], new_description=snap["old_description"])
        await update.message.reply_text(f"Reverted -- description is back to \"{row['description']}\".")
    elif action == "edit_category":
        row = db.edit_expense(chat_id, snap["expense_id"], new_category=snap["old_category"])
        await update.message.reply_text(f"Reverted -- category is back to [{row['category']}].")
    return True


SIMPLE_DOMAIN_ACTIONS = {"edit_date", "delete"}

# Registry for the three non-expense domains, which all share the same
# narrower correction surface (see _handle_simple_domain_correction).
_DOMAIN_OPS = {
    "meal": {"noun": "meal", "recent_cmd": "/recentmeals", "date_field": "meal_date",
              "get": db.get_meal, "edit_date": db.edit_meal_date, "delete": db.delete_meal,
              "restore": db.restore_deleted_meal, "line": _meal_line},
    "workout": {"noun": "workout", "recent_cmd": "/recentworkouts", "date_field": "workout_date",
                 "get": db.get_workout, "edit_date": db.edit_workout_date, "delete": db.delete_workout,
                 "restore": db.restore_deleted_workout, "line": _workout_line},
    "vitals": {"noun": "check-in", "recent_cmd": "/recentvitals", "date_field": "vitals_date",
                "get": db.get_vitals, "edit_date": db.edit_vitals_date, "delete": db.delete_vitals,
                "restore": db.restore_deleted_vitals, "line": _vitals_line},
}


async def _handle_simple_domain_correction(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                            parsed: dict, domain: str, recent_ids: set):
    """Corrections for meal/workout/vitals targets are intentionally
    narrower than expenses right now -- only moving the date or deleting a
    duplicate/mistake, which covers the flagship case ("that log from
    yesterday was wrong, tag it to the day before instead") without needing
    a field-level /editmeal-style command yet for any of the three."""
    ops = _DOMAIN_OPS[domain]
    chat_id = update.effective_chat.id
    target_id = parsed.get("target_expense_id")
    action = parsed.get("correction_action")
    noun, recent_cmd = ops["noun"], ops["recent_cmd"]

    if target_id not in recent_ids or action not in SIMPLE_DOMAIN_ACTIONS:
        await update.message.reply_text(
            f"I'm not sure which {noun} you mean, or that kind of edit isn't supported yet for "
            f"{noun}s -- only moving the date or deleting one is. Run {recent_cmd} to see recent entries."
        )
        return

    row = ops["get"](chat_id, target_id)
    if row is None:
        await update.message.reply_text(f"Couldn't find that {noun} anymore -- run {recent_cmd} to check.")
        return

    if action == "edit_date":
        days_ago = parsed.get("days_ago")
        if not isinstance(days_ago, int) or not (0 <= days_ago <= 14):
            await update.message.reply_text(
                f"Which day did you mean for that {noun}? (e.g. today, yesterday, or '3 days ago')"
            )
            return
        today = date.fromisoformat(db.today_str())
        new_date = today - timedelta(days=days_ago)
        old_date = row[ops["date_field"]]
        updated = ops["edit_date"](chat_id, target_id, new_date.isoformat())
        context.chat_data[LAST_CORRECTION_KEY] = {
            "domain": domain, "action": "edit_date", "expense_id": target_id, "old_date": old_date,
        }
        await update.message.reply_text(
            f"Updated -- that {noun} is now dated {updated[ops['date_field']]} (was {old_date}). "
            "Reply 'undo' if that's wrong."
        )
        return

    if action == "delete":
        deleted = ops["delete"](chat_id, target_id)
        context.chat_data[LAST_CORRECTION_KEY] = {"domain": domain, "action": "delete", "row": deleted}
        await update.message.reply_text(f"Deleted: {ops['line'](deleted)}. Reply 'undo' if that's wrong.")
        return


async def _handle_correction(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict,
                              recent_ids: set, recent_meal_ids: set = frozenset(),
                              recent_workout_ids: set = frozenset(), recent_vitals_ids: set = frozenset()):
    """Applies a correction the AI identified against one of the chat's
    recent expenses/meals/workouts/vitals. Every confirmation message here
    is built from real values just read back from the database -- never
    from AI-generated text -- so the bot can never claim to have made a
    change it didn't actually make (the exact failure mode that prompted
    this feature)."""
    chat_id = update.effective_chat.id
    target_id = parsed.get("target_expense_id")
    action = parsed.get("correction_action")
    domain = parsed.get("target_domain") or "expense"

    if domain == "meal":
        await _handle_simple_domain_correction(update, context, parsed, "meal", recent_meal_ids)
        return
    if domain == "workout":
        await _handle_simple_domain_correction(update, context, parsed, "workout", recent_workout_ids)
        return
    if domain == "vitals":
        await _handle_simple_domain_correction(update, context, parsed, "vitals", recent_vitals_ids)
        return

    if target_id not in recent_ids or action not in CORRECTION_ACTIONS:
        await update.message.reply_text(
            "I'm not sure which expense you mean -- run /recent to see IDs, then use /edit <id> or /delete <id>."
        )
        return

    row = db.get_expense(chat_id, target_id)
    if row is None:
        await update.message.reply_text("Couldn't find that expense anymore -- run /recent to check.")
        return

    if action == "edit_date":
        days_ago = parsed.get("days_ago")
        if not isinstance(days_ago, int) or not (0 <= days_ago <= 14):
            await update.message.reply_text(
                f"Which day did you mean for {_money(row['amount'], row['currency'])} -- "
                f"{row['description']}? (e.g. today, yesterday, or '3 days ago')"
            )
            return
        today = date.fromisoformat(db.today_str())
        new_date = today - timedelta(days=days_ago)
        old_date = row["expense_date"]
        updated = db.edit_expense_date(chat_id, target_id, new_date.isoformat())
        context.chat_data[LAST_CORRECTION_KEY] = {
            "action": "edit_date", "expense_id": target_id, "old_date": old_date,
        }
        await update.message.reply_text(
            f"Updated -- {_money(updated['amount'], updated['currency'])} \"{updated['description']}\" is "
            f"now dated {updated['expense_date']} (was {old_date}). Reply 'undo' if that's wrong."
        )
        return

    if action == "edit_currency":
        new_currency = fx.normalize_currency(parsed.get("new_currency"))
        if not new_currency:
            await update.message.reply_text(
                f"What currency should {_money(row['amount'], row['currency'])} -- "
                f"{row['description']} actually be?"
            )
            return
        old_currency = row["currency"]
        old_display = _money(row["amount"], old_currency)
        updated = db.edit_expense(chat_id, target_id, new_currency=new_currency)
        context.chat_data[LAST_CORRECTION_KEY] = {
            "action": "edit_currency", "expense_id": target_id, "old_currency": old_currency,
        }
        await update.message.reply_text(
            f"Updated -- \"{updated['description']}\" is now {_money(updated['amount'], updated['currency'])} "
            f"(was {old_display}). Reply 'undo' if that's wrong."
        )
        return

    if action == "edit_amount":
        new_amount = parsed.get("new_amount")
        if not isinstance(new_amount, (int, float)) or new_amount <= 0:
            await update.message.reply_text(f"What should the amount for \"{row['description']}\" actually be?")
            return
        old_display = _money(row["amount"], row["currency"])
        updated = db.edit_expense(chat_id, target_id, new_amount=float(new_amount))
        context.chat_data[LAST_CORRECTION_KEY] = {
            "action": "edit_amount", "expense_id": target_id, "old_amount": row["amount"],
        }
        await update.message.reply_text(
            f"Updated -- \"{updated['description']}\" is now {_money(updated['amount'], updated['currency'])} "
            f"(was {old_display}). Reply 'undo' if that's wrong."
        )
        return

    if action == "edit_description":
        new_description = parsed.get("new_description")
        if not new_description:
            await update.message.reply_text("What should the description say instead?")
            return
        old_description = row["description"]
        updated = db.edit_expense(chat_id, target_id, new_description=new_description)
        context.chat_data[LAST_CORRECTION_KEY] = {
            "action": "edit_description", "expense_id": target_id, "old_description": old_description,
        }
        await update.message.reply_text(
            f"Updated -- description is now \"{updated['description']}\" (was \"{old_description}\"). "
            "Reply 'undo' if that's wrong."
        )
        return

    if action == "edit_category":
        new_category = parsed.get("new_category")
        if new_category not in config.CATEGORIES:
            await update.message.reply_text(
                f"What category should \"{row['description']}\" actually be? "
                f"({', '.join(config.CATEGORIES)})"
            )
            return
        old_category = row["category"]
        updated = db.edit_expense(chat_id, target_id, new_category=new_category)
        context.chat_data[LAST_CORRECTION_KEY] = {
            "action": "edit_category", "expense_id": target_id, "old_category": old_category,
        }
        await update.message.reply_text(
            f"Updated -- \"{updated['description']}\" is now [{updated['category']}] (was [{old_category}]). "
            "Reply 'undo' if that's wrong."
        )
        return

    if action == "delete":
        deleted = db.delete_expense(chat_id, target_id)
        context.chat_data[LAST_CORRECTION_KEY] = {"action": "delete", "row": deleted}
        await update.message.reply_text(
            f"Deleted: {_money(deleted['amount'], deleted['currency'])} -- {deleted['description']} "
            f"[{deleted['category']}] ({deleted['expense_date']}). Reply 'undo' if that's wrong."
        )
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    pending = context.chat_data.get(PENDING_KEY)

    # Only treat a bare "no"/"wrong"/"undo" as reverting a correction when
    # we're not mid-clarification -- otherwise it's very likely a genuine
    # answer to whatever question was just asked (e.g. "is this claimable?").
    if not pending and text.lower().rstrip(".!") in CORRECTION_UNDO_PHRASES:
        if await _revert_last_correction(update, context):
            return
        # nothing to revert -- fall through and let it be parsed normally

    # A correction snapshot only survives for the single reply immediately
    # following it; anything else means it's no longer relevant.
    context.chat_data.pop(LAST_CORRECTION_KEY, None)

    recent_expenses = _recent_for_ai(chat_id)
    recent_ids = {r["id"] for r in recent_expenses}
    recent_meals = _recent_meals_for_ai(chat_id)
    recent_meal_ids = {r["id"] for r in recent_meals}
    recent_workouts = _recent_workouts_for_ai(chat_id)
    recent_workout_ids = {r["id"] for r in recent_workouts}
    recent_vitals = _recent_vitals_for_ai(chat_id)
    recent_vitals_ids = {r["id"] for r in recent_vitals}

    if pending:
        # We asked a clarifying question; treat this message as the answer.
        merged_text = f"{pending['original']}\n(Additional info: {text})"
        parsed = ai.parse_message(merged_text, recent_expenses, recent_meals, recent_workouts, recent_vitals)
    else:
        merged_text = text
        parsed = ai.parse_message(text, recent_expenses, recent_meals, recent_workouts, recent_vitals)

    intent = parsed.get("intent")

    if intent == "clarification":
        # Carry forward the ACCUMULATED text, not just this latest fragment --
        # otherwise a second (or third) round of clarification silently drops
        # everything learned in earlier rounds (e.g. an amount mentioned two
        # messages ago), which was a real bug: the context would shrink with
        # every back-and-forth instead of growing.
        context.chat_data[PENDING_KEY] = {"original": merged_text}
        await update.message.reply_text(parsed.get("clarification_question") or "Could you clarify that?")
        return

    if intent == "correction":
        context.chat_data.pop(PENDING_KEY, None)
        await _handle_correction(update, context, parsed, recent_ids, recent_meal_ids,
                                  recent_workout_ids, recent_vitals_ids)
        return

    if intent == "show_balance":
        # Answered directly with real numbers -- the exact same code path as
        # /balance -- rather than just telling the user to go type /balance.
        context.chat_data.pop(PENDING_KEY, None)
        await update.message.reply_text(_balance_text(chat_id))
        return

    if intent == "show_recent":
        context.chat_data.pop(PENDING_KEY, None)
        await update.message.reply_text(_recent_text(chat_id))
        return

    if intent == "log_meal":
        context.chat_data.pop(PENDING_KEY, None)
        await _log_meal_and_reply(update, chat_id, parsed)
        return

    if intent == "log_workout":
        context.chat_data.pop(PENDING_KEY, None)
        workout_id = db.add_workout(chat_id, parsed.get("activity"), parsed.get("duration_min"),
                                     parsed.get("distance_km"), parsed.get("workout_notes"))
        row = db.get_workout(chat_id, workout_id)
        await update.message.reply_text(f"Logged: {_workout_line(row)}")
        return

    if intent == "log_vitals":
        context.chat_data.pop(PENDING_KEY, None)
        await _log_vitals_and_reply(update, chat_id, parsed)
        return

    if intent != "log_expense":
        # "casual", or anything the model didn't tag cleanly -- never silent.
        if pending:
            context.chat_data.pop(PENDING_KEY, None)
        reply = parsed.get("casual_reply") or (
            "Not sure what to do with that. Use /log, /claim, /logmeal, /logworkout, /logvitals, /balance, "
            "/summary, /recent, or /help."
        )
        await update.message.reply_text(reply)
        return

    context.chat_data.pop(PENDING_KEY, None)

    items = parsed.get("expenses") or []
    items = [it for it in items if it.get("amount") is not None]
    if not items:
        await update.message.reply_text("I still didn't catch an amount -- try e.g. 'spent 12 on lunch'.")
        return

    logged_lines = []
    any_personal = False
    for item in items:
        amount = float(item["amount"])
        currency = fx.normalize_currency(item.get("currency"))
        description = item.get("description") or "expense"
        category = item.get("category") or ai.categorize(description)
        is_claimable = bool(item.get("is_claimable"))
        db.add_expense(chat_id, amount, currency, description, category, is_claimable=is_claimable)
        tag = " [claimable]" if is_claimable else ""
        logged_lines.append(f"{_money(amount, currency)} -- {description} [{category}]{tag}")
        any_personal = any_personal or not is_claimable

    status = db.get_status(chat_id)
    header = "Logged:" if len(logged_lines) == 1 else f"Logged {len(logged_lines)} expenses:"
    body = "\n".join(logged_lines) if len(logged_lines) == 1 else "\n".join(f"- {l}" for l in logged_lines)
    await update.message.reply_text(f"{header}\n{body}\n\n{_status_text(status)}")
    if any_personal:
        await _send_alert_if_needed(update, chat_id)


# ---------- global error handler ----------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Safety net: any exception a handler doesn't catch itself lands here.
    Without this, python-telegram-bot just logs it and the user gets dead
    silence -- this makes sure they always get *some* reply."""
    logger.error("Unhandled exception while processing update: %s", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Something went wrong on my end processing that -- try again in a moment."
            )
        except Exception:
            logger.exception("Failed to notify user about the error")


# ---------- background rollover check ----------

async def rollover_tick(context: ContextTypes.DEFAULT_TYPE):
    """Runs periodically so rollovers happen even if no one messages the bot
    right at midnight. Notifies each user only if a rollover actually occurred."""
    for chat_id in db.get_all_chat_ids():
        result = db.ensure_rollover(chat_id)
        if not result["rollovers"]:
            continue
        status = db.get_status(chat_id)
        lines = [f"{d}: leftover {_money(l)}" for d, l in result["rollovers"]]
        text = "New day, balance rolled over:\n" + "\n".join(lines) + f"\n\n{_status_text(status)}"
        if result["new_best_streak"]:
            text += f"\n\nNew personal best streak: {result['new_best_streak']} days within budget!"
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.exception("Failed to notify chat %s of rollover", chat_id)


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. See .env.example.")
    if not config.ANTHROPIC_API_KEY:
        raise SystemExit("ANTHROPIC_API_KEY is not set. See .env.example.")

    db.init_db()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settarget", settarget))
    app.add_handler(CommandHandler("log", log_expense))
    app.add_handler(CommandHandler("claim", claim_expense))
    app.add_handler(CommandHandler("claimed", claimed))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CommandHandler("logmeal", logmeal_cmd))
    app.add_handler(CommandHandler("recentmeals", recentmeals))
    app.add_handler(CommandHandler("logworkout", logworkout_cmd))
    app.add_handler(CommandHandler("recentworkouts", recentworkouts))
    app.add_handler(CommandHandler("logvitals", logvitals_cmd))
    app.add_handler(CommandHandler("recentvitals", recentvitals))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    if app.job_queue is not None:
        app.job_queue.run_repeating(rollover_tick, interval=3600, first=10)
    else:
        logger.warning(
            "JobQueue not available -- install with pip install python-telegram-bot[job-queue] "
            "to get automatic daily rollover notifications. Rollover math still runs correctly "
            "whenever a user interacts with the bot."
        )

    logger.info("Bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
