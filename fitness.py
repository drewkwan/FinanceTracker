"""
Workout logging: /logworkout, /recentworkouts.
"""

from telegram import Update
from telegram.ext import ContextTypes

import ai
import db
from access import _reject_if_not_allowed
from formatting import _workout_line


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
