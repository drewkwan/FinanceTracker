"""
Workout logging: /logworkout, /recentworkouts, and (via _log_workout_and_reply)
the workout branch of photo logging -- see nutrition.handle_photo.
"""

from telegram import Update
from telegram.ext import ContextTypes

import ai
import db
from access import _reject_if_not_allowed
from formatting import _daily_calorie_balance_text, _workout_line
from replies import _reply


async def _log_workout_and_reply(update: Update, chat_id: int, data: dict):
    """Shared by /logworkout and the photo-logging workout branch -- one
    insert, one reply shape (mirrors nutrition._log_meal_and_reply's own
    reasoning). When calories_burned is present (typically from a fitness
    app/wearable screenshot, not a typed description), the reply also shows
    it against today's calories eaten -- logging calories burned in
    isolation, with nothing to compare it to, isn't the point of tracking it."""
    workout_id = db.add_workout(chat_id, data.get("activity"), data.get("duration_min"),
                                 data.get("distance_km"), data.get("notes"),
                                 calories_burned=data.get("calories_burned"))
    row = db.get_workout(chat_id, workout_id)
    reply = f"Logged: {_workout_line(row)}"
    if row.get("calories_burned"):
        reply += f"\n\n{_daily_calorie_balance_text(chat_id)}"
    await _reply(update, chat_id, reply)


async def logworkout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    description = " ".join(context.args)
    if not description:
        await update.message.reply_text("Usage: /logworkout tennis for an hour")
        return
    data = ai.extract_workout(description)
    await _log_workout_and_reply(update, chat_id, data)


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
