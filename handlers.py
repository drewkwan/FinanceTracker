"""
The free-text chat surface: one message in, one intent-routed reply out.
This is the single busiest call path in the bot -- ai.parse_message
classifies + extracts in one call, then this module dispatches to whichever
domain module actually owns the effect (finance/nutrition/fitness/vitals/
tasks/memory/correction/rundown), never duplicating their logic. Also home
to the global error handler, which is the last line of defense against a
silent failure reaching the user.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

import ai
import db
import fx
from access import _reject_if_not_allowed
from correction import CORRECTION_UNDO_PHRASES, LAST_CORRECTION_KEY, _handle_correction, _revert_last_correction
from finance import _balance_text, _recent_text
from fitness import _force_log_workout_and_reply
from formatting import _money, _status_text, _workout_line
from memory import _memory_for_ai, _memory_text
from nutrition import _log_meals_and_reply
from events import _add_events_and_reply, _events_text
from reminders import _add_reminder_and_reply, _reminders_text
from replies import PENDING_DUPLICATE_WORKOUT_KEY, PENDING_KEY, _reply, _send_alert_if_needed
from rundown import _rundown_reply_text
from tasks import _log_tasks_and_reply, _recent_tasks_for_ai, _tasks_text
from vitals import _log_vitals_and_reply

logger = logging.getLogger(__name__)

RECENT_EXPENSES_FOR_AI = 8  # how much history the model gets to resolve "that", "the duplicate", etc.
RECENT_MESSAGES_FOR_AI = 30  # rolling conversation window -- see db.py's module docstring on messages vs memory


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


def _recent_messages_for_ai(chat_id: int) -> list:
    rows = db.get_recent_messages(chat_id, limit=RECENT_MESSAGES_FOR_AI)
    return [{"role": r["role"], "content": r["content"]} for r in rows]


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # A pending duplicate-workout question (see fitness._ask_about_duplicate_workout)
    # is resolved directly here, deterministically, rather than falling through to
    # the general ai.parse_message pipeline below -- that pipeline's "log_workout"
    # shape has nowhere to carry calories_burned (see ai.py's PARSE_SYSTEM_PROMPT),
    # so re-parsing this reply as free text would silently drop the exact number
    # the photo already gave us. We already have the fully-parsed photo data
    # (stashed by fitness.py); all that's needed here is yes or no.
    dup_pending = context.chat_data.pop(PENDING_DUPLICATE_WORKOUT_KEY, None)
    if dup_pending:
        db.add_message(chat_id, "user", text)
        affirmative = any(
            w in text.lower()
            for w in ("yes", "separate", "different", "another", "anyway", "log it", "do log", "keep both")
        )
        if affirmative:
            await _force_log_workout_and_reply(update, chat_id, dup_pending["data"], dup_pending["workout_date"])
        else:
            await _reply(update, chat_id, "Got it -- skipped, since it looked like the same workout logged twice.")
        return

    pending = context.chat_data.get(PENDING_KEY)
    db.add_message(chat_id, "user", text)

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
    recent_tasks = _recent_tasks_for_ai(chat_id)
    recent_task_ids = {r["id"] for r in recent_tasks}
    # The message just added above is deliberately included here -- the
    # model should see its own current turn as part of the running thread,
    # not just what came before it.
    recent_messages = _recent_messages_for_ai(chat_id)
    memory_list = _memory_for_ai(chat_id)

    if pending:
        # We asked a clarifying question; treat this message as the answer.
        merged_text = f"{pending['original']}\n(Additional info: {text})"
        parsed = ai.parse_message(merged_text, recent_expenses, recent_meals, recent_workouts, recent_vitals,
                                   recent_tasks, recent_messages, memory_list)
    else:
        merged_text = text
        parsed = ai.parse_message(text, recent_expenses, recent_meals, recent_workouts, recent_vitals,
                                   recent_tasks, recent_messages, memory_list)

    intent = parsed.get("intent")

    if intent == "clarification":
        # Carry forward the ACCUMULATED text, not just this latest fragment --
        # otherwise a second (or third) round of clarification silently drops
        # everything learned in earlier rounds (e.g. an amount mentioned two
        # messages ago), which was a real bug: the context would shrink with
        # every back-and-forth instead of growing.
        context.chat_data[PENDING_KEY] = {"original": merged_text}
        await _reply(update, chat_id, parsed.get("clarification_question") or "Could you clarify that?")
        return

    if intent == "correction":
        context.chat_data.pop(PENDING_KEY, None)
        # Deliberately logged BEFORE dispatch, not just on failure -- the
        # generic "I'm not sure which X you mean" reply gives no visibility
        # into whether the AI actually got the domain/id/action right (a
        # real diagnostic gap: a user reported "17 done" failing to mark a
        # to-do done with no way to tell, from the reply alone, whether
        # parse_message returned the wrong id, the wrong action, or the
        # right values against a recent-id set that didn't contain them).
        logger.info(
            "correction parsed: chat_id=%s text=%r target_domain=%r target_expense_id=%r "
            "correction_action=%r recent_task_ids=%s",
            chat_id, merged_text, parsed.get("target_domain"), parsed.get("target_expense_id"),
            parsed.get("correction_action"), sorted(recent_task_ids),
        )
        await _handle_correction(update, context, parsed, recent_ids, recent_meal_ids,
                                  recent_workout_ids, recent_vitals_ids, recent_task_ids)
        return

    if intent == "show_balance":
        # Answered directly with real numbers -- the exact same code path as
        # /balance -- rather than just telling the user to go type /balance.
        context.chat_data.pop(PENDING_KEY, None)
        await _reply(update, chat_id, _balance_text(chat_id))
        return

    if intent == "show_recent":
        context.chat_data.pop(PENDING_KEY, None)
        await _reply(update, chat_id, _recent_text(chat_id))
        return

    if intent == "rundown":
        # Cross-domain synthesis (section 04 of the plan) -- real 7-day
        # figures computed in code, handed to Claude only to narrate, same
        # "never let the model guess a number" discipline as show_balance.
        context.chat_data.pop(PENDING_KEY, None)
        await _reply(update, chat_id, await _rundown_reply_text(chat_id))
        return

    if intent == "log_meal":
        context.chat_data.pop(PENDING_KEY, None)
        meals = parsed.get("meals") or []
        if not meals:
            await _reply(update, chat_id, "I didn't catch what you ate -- try describing it again.")
            return
        await _log_meals_and_reply(update, chat_id, meals)
        return

    if intent == "log_workout":
        context.chat_data.pop(PENDING_KEY, None)
        workout_id = db.add_workout(chat_id, parsed.get("activity"), parsed.get("duration_min"),
                                     parsed.get("distance_km"), parsed.get("workout_notes"))
        row = db.get_workout(chat_id, workout_id)
        await _reply(update, chat_id, f"Logged: {_workout_line(row)}")
        return

    if intent == "log_vitals":
        context.chat_data.pop(PENDING_KEY, None)
        await _log_vitals_and_reply(update, chat_id, parsed)
        return

    if intent == "log_task":
        context.chat_data.pop(PENDING_KEY, None)
        tasks = parsed.get("tasks") or []
        if not tasks:
            await _reply(update, chat_id, "I didn't catch what to add -- try describing the to-do again.")
            return
        await _log_tasks_and_reply(update, chat_id, tasks)
        return

    if intent == "show_tasks":
        context.chat_data.pop(PENDING_KEY, None)
        await _reply(update, chat_id, _tasks_text(chat_id))
        return

    if intent == "add_reminder":
        context.chat_data.pop(PENDING_KEY, None)
        description = parsed.get("reminder_description")
        if not description:
            await _reply(update, chat_id, "What should I remind you about every day?")
            return
        await _add_reminder_and_reply(update, chat_id, description)
        return

    if intent == "show_reminders":
        context.chat_data.pop(PENDING_KEY, None)
        await _reply(update, chat_id, _reminders_text(chat_id))
        return

    if intent == "add_event":
        context.chat_data.pop(PENDING_KEY, None)
        events = parsed.get("events") or []
        if not events:
            await _reply(update, chat_id, "I didn't catch what to schedule -- try describing it again.")
            return
        await _add_events_and_reply(update, chat_id, events)
        return

    if intent == "show_events":
        context.chat_data.pop(PENDING_KEY, None)
        await _reply(update, chat_id, _events_text(chat_id))
        return

    if intent == "remember":
        context.chat_data.pop(PENDING_KEY, None)
        label = parsed.get("memory_label")
        content = parsed.get("memory_content")
        if not label or not content:
            await _reply(update, chat_id, "What should I remember, and what should I call it?")
            return
        db.set_memory(chat_id, label, content, parsed.get("memory_category"))
        await _reply(update, chat_id, f"Got it -- I'll remember \"{label}\": {content}")
        return

    if intent == "forget":
        context.chat_data.pop(PENDING_KEY, None)
        label = parsed.get("memory_label")
        deleted = db.delete_memory_by_label(chat_id, label) if label else None
        if not deleted:
            await _reply(update, chat_id, "I couldn't find that in what I remember -- run /memory to see the list.")
            return
        context.chat_data[LAST_CORRECTION_KEY] = {"domain": "memory", "action": "delete", "row": deleted}
        await _reply(update, chat_id, f"Forgot \"{deleted['label']}\". Reply 'undo' if that's wrong.")
        return

    if intent == "show_memory":
        # Same discipline as show_balance/show_recent -- the real saved
        # list, not the model's best guess at what it remembers.
        context.chat_data.pop(PENDING_KEY, None)
        await _reply(update, chat_id, _memory_text(chat_id))
        return

    if intent != "log_expense":
        # "casual", or anything the model didn't tag cleanly -- never silent.
        if pending:
            context.chat_data.pop(PENDING_KEY, None)
        reply = parsed.get("casual_reply") or (
            "Not sure what to do with that. Use /log, /claim, /logmeal, /logworkout, /logvitals, /balance, "
            "/summary, /recent, or /help."
        )
        await _reply(update, chat_id, reply)
        return

    context.chat_data.pop(PENDING_KEY, None)

    items = parsed.get("expenses") or []
    items = [it for it in items if it.get("amount") is not None]
    if not items:
        await _reply(update, chat_id, "I still didn't catch an amount -- try e.g. 'spent 12 on lunch'.")
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
    body = "\n".join(logged_lines) if len(logged_lines) == 1 else "\n".join(f"- {ln}" for ln in logged_lines)
    await _reply(update, chat_id, f"{header}\n{body}\n\n{_status_text(status)}")
    if any_personal:
        await _send_alert_if_needed(update, chat_id)


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
