"""
Meal logging: /logmeal, photo logging, /recentmeals. Meals are logged as
items, not meal slots -- see the module docstring in ai.py for why.
"""

from telegram import Update
from telegram.ext import ContextTypes

import ai
import db
from access import _reject_if_not_allowed
from formatting import _calorie_range, _daily_meal_totals_text, _meal_line
from replies import _reply


async def _log_meal_and_reply(update: Update, chat_id: int, data: dict):
    """Shared by /logmeal, photo logging, and the natural-language log_meal
    intent -- one insert, one reply shape, so all three paths are
    guaranteed to say the same thing (see finance._balance_text's docstring
    for the same reasoning applied to /balance)."""
    meal_id = db.add_meal(
        chat_id, data.get("meal_type"), data.get("items") or data.get("meal_items"),
        data.get("calories_low"), data.get("calories_high"), data.get("calories_estimate"),
        water_ml=data.get("water_ml"),
    )
    row = db.get_meal(chat_id, meal_id)
    items = ", ".join(row["items"]) or "meal"
    water_line = f"\nWater: +{row['water_ml']:.0f}ml" if row.get("water_ml") else ""
    await _reply(
        update, chat_id,
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
