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
        "Hey! I'll track your daily spending allowance and claimable expenses.\n\n"
        "Set a daily target: /settarget 100\n"
        "Log a personal expense: /log 12.50 lunch\n"
        "Log in another currency: /log 20 USD taxi\n"
        "Log a claimable one: /claim 300 groceries\n"
        "Cleared a reimbursement: /claimed\n"
        "Check where you stand: /balance\n"
        "Get a breakdown: /summary week\n"
        "See recent entries: /recent\n"
        "Fix a mistake: /undo, /edit <id> <amount>, or /delete <id>\n\n"
        "Or just tell me naturally, e.g. \"spent 15 on uber\"."
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


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    status = db.get_status(chat_id)
    text = _status_text(status)
    pending = db.get_pending_claimables(chat_id)
    if pending:
        lines = [f"  #{p['id']} {_money(p['amount'], p['currency'])} -- {p['description']} [{p['category']}]"
                  for p in pending]
        text += "\n\nPending claimables:\n" + "\n".join(lines)
    await update.message.reply_text(text)


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
    rows = db.get_recent_expenses(chat_id, limit=limit)
    if not rows:
        await update.message.reply_text("No expenses logged yet.")
        return
    lines = [_expense_line(r) for r in rows]
    await update.message.reply_text("Recent expenses:\n" + "\n".join(lines))


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


WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    period = (context.args[0].lower() if context.args else "week")
    today = date.today()
    tomorrow = today + timedelta(days=1)

    if period == "today":
        length = 1
    elif period == "month":
        period, length = "month", 30
    else:
        period, length = "week", 7
    # current period is exactly `length` days ending today; previous period is
    # the `length` days immediately before that -- equal-length, non-overlapping.
    current_start = today - timedelta(days=length - 1)
    current_end = tomorrow
    prev_start = current_start - timedelta(days=length)
    prev_end = current_start

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


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    pending = context.chat_data.get(PENDING_KEY)
    if pending:
        # We asked a clarifying question; treat this message as the answer.
        merged_text = f"{pending['original']}\n(Additional info: {text})"
        parsed = ai.parse_message(merged_text)
    else:
        parsed = ai.parse_message(text)

    # Check for a clarification question first -- this also covers the "couldn't
    # parse it" / "AI call failed" fallbacks, which set is_expense=False but still
    # carry a specific, more useful message than the generic one below.
    if parsed.get("needs_clarification") and parsed.get("clarification_question"):
        context.chat_data[PENDING_KEY] = {"original": text}
        await update.message.reply_text(parsed["clarification_question"])
        return

    if not parsed.get("is_expense"):
        if pending:
            context.chat_data.pop(PENDING_KEY, None)
        # Casual, non-expense chat (e.g. "hi", "thanks") gets a natural reply from
        # the model itself rather than a canned line. Only fall back to the generic
        # message if the model didn't give us one (e.g. the AI-call-failed fallback).
        reply = parsed.get("casual_reply") or (
            "Not sure what to do with that. Use /log, /claim, /balance, /summary, /recent, or /help."
        )
        await update.message.reply_text(reply)
        return

    context.chat_data.pop(PENDING_KEY, None)

    amount = parsed.get("amount")
    if amount is None:
        await update.message.reply_text("I still didn't catch an amount -- try e.g. 'spent 12 on lunch'.")
        return

    currency = fx.normalize_currency(parsed.get("currency"))
    description = parsed.get("description") or "expense"
    category = parsed.get("category") or ai.categorize(description)
    is_claimable = bool(parsed.get("is_claimable"))

    db.add_expense(chat_id, float(amount), currency, description, category, is_claimable=is_claimable)
    status = db.get_status(chat_id)

    if is_claimable:
        await update.message.reply_text(
            f"Logged claimable: {_money(amount, currency)} -- {description} [{category}]\n"
            f"Pending claimables: {_money(status['pending_claimable'])}\n"
            "(This doesn't touch your daily allowance.)"
        )
    else:
        await update.message.reply_text(
            f"Logged: {_money(amount, currency)} -- {description} [{category}]\n\n{_status_text(status)}"
        )
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
