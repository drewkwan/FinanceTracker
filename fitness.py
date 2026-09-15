"""
Workout logging: /logworkout, /recentworkouts, and (via _log_workout_and_reply)
the workout branch of photo logging -- see nutrition.handle_photo.
"""

from datetime import date, timedelta

from telegram import Update
from telegram.ext import ContextTypes

import ai
import db
from access import _reject_if_not_allowed
from formatting import _daily_calorie_balance_text, _workout_line
from replies import PENDING_DUPLICATE_WORKOUT_KEY, _reply


def _find_duplicate_workout(chat_id: int, target_date: str, calories_burned: float | None) -> dict | None:
    """Two photos of the same underlying fitness-app data (e.g. a Move-goal
    screen and a daily-activity screen, both describing the same day) got
    logged as TWO separate workouts, silently doubling the day's
    calories-burned total -- a real bug (see nutrition.handle_photo's
    docstring). Only meaningful when calories_burned is present (i.e. from a
    photo -- a typed /logworkout essentially never reports one, so this is a
    no-op for that path), and only flags an actual MATCH: a small tolerance
    (2%, minimum 5 kcal) absorbs rounding differences between two screens of
    the SAME data, not two genuinely different workouts that happen to burn
    a similar amount."""
    if not calories_burned:
        return None
    next_day = (date.fromisoformat(target_date) + timedelta(days=1)).isoformat()
    tolerance = max(5.0, 0.02 * calories_burned)
    for w in db.get_workouts_in_range(chat_id, target_date, next_day):
        existing = w.get("calories_burned")
        if existing and abs(existing - calories_burned) <= tolerance:
            return w
    return None


async def _ask_about_duplicate_workout(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                        data: dict, existing: dict, workout_date: str | None):
    """Mirrors nutrition._ask_about_caption_extra_item's reasoning: rather
    than guess whether this is really a second workout or the same
    fitness-app data seen in a second screenshot, ask -- but resolve the
    answer deterministically from the already-parsed photo data (stashed in
    PENDING_DUPLICATE_WORKOUT_KEY) rather than through the general
    ai.parse_message text pipeline, which has no field for calories_burned
    on its log_workout shape and would silently drop the exact number the
    photo already gave us (see replies.PENDING_DUPLICATE_WORKOUT_KEY)."""
    cal = data.get("calories_burned")
    today = db.today_str()
    when = "today" if (workout_date or today) == today else f"on {workout_date}"
    context.chat_data[PENDING_DUPLICATE_WORKOUT_KEY] = {"data": data, "workout_date": workout_date}
    await _reply(
        update, chat_id,
        f"That ~{cal:.0f} kcal burned {when} looks like it might be the same data as {_workout_line(existing)} "
        "already logged -- is this a separate workout, or the same one shown again? Tell me and I'll log it "
        "(or skip it)."
    )


async def _reply_workout_logged(update: Update, chat_id: int, row: dict, workout_date: str | None):
    reply = f"Logged: {_workout_line(row)}"
    if row.get("calories_burned"):
        reply += f"\n\n{_daily_calorie_balance_text(chat_id, workout_date)}"
    await _reply(update, chat_id, reply)


async def _force_log_workout_and_reply(update: Update, chat_id: int, data: dict, workout_date: str | None = None):
    """Logs unconditionally, skipping _find_duplicate_workout -- used only
    to resolve a duplicate-workout clarification the user has already
    answered (see handlers.handle_text's PENDING_DUPLICATE_WORKOUT_KEY
    handling): the user just confirmed this really is a separate workout,
    so re-running the same check would just ask the same question again."""
    workout_id = db.add_workout(chat_id, data.get("activity"), data.get("duration_min"),
                                 data.get("distance_km"), data.get("notes"),
                                 calories_burned=data.get("calories_burned"), workout_date=workout_date)
    row = db.get_workout(chat_id, workout_id)
    await _reply_workout_logged(update, chat_id, row, workout_date)


async def _log_workout_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, data: dict,
                                  workout_date: str | None = None):
    """Shared by /logworkout and the photo-logging workout branch -- one
    insert, one reply shape (mirrors nutrition._log_meal_and_reply's own
    reasoning). When calories_burned is present (typically from a fitness
    app/wearable screenshot, not a typed description), the reply also shows
    it against that day's calories eaten -- logging calories burned in
    isolation, with nothing to compare it to, isn't the point of tracking it.
    workout_date lets a caller that already computed a real backdated date
    (see nutrition.handle_photo's use of ai's logged_days_ago) log directly
    onto the right day instead of defaulting to today and needing a
    follow-up correction -- but before logging, a calories_burned match
    against that SAME date is checked first (see _find_duplicate_workout) so
    two photos of the same underlying data don't silently double the day's
    total."""
    calories_burned = data.get("calories_burned")
    duplicate = _find_duplicate_workout(chat_id, workout_date or db.today_str(), calories_burned)
    if duplicate:
        await _ask_about_duplicate_workout(update, context, chat_id, data, duplicate, workout_date)
        return
    await _force_log_workout_and_reply(update, chat_id, data, workout_date)


async def logworkout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    description = " ".join(context.args)
    if not description:
        await update.message.reply_text("Usage: /logworkout tennis for an hour")
        return
    data = ai.extract_workout(description)
    await _log_workout_and_reply(update, context, chat_id, data)


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
