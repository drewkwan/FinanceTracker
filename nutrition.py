"""
Meal logging: /logmeal, photo logging, /recentmeals. Meals are logged as
items, not meal slots -- see the module docstring in ai.py for why.
"""

from telegram import Update
from telegram.ext import ContextTypes

import ai
import db
from access import _reject_if_not_allowed
from fitness import _log_workout_and_reply
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


async def _log_meals_and_reply(update: Update, chat_id: int, meals: list):
    """Entry point for the natural-language log_meal intent, which can name
    more than one meal in a single message (e.g. "for breakfast: toast and
    coffee. For lunch: noodles and a latte") -- this used to be the real bug:
    parse_message only had room for ONE meal's fields, so a second meal
    mentioned in the same message was silently dropped rather than logged.
    ai.py now always returns a list ("meals"), mirroring log_expense's
    existing multi-item discipline.

    A single meal reuses _log_meal_and_reply's exact wording/behavior
    unchanged (same reply shape existing callers/tests expect); more than
    one meal gets ONE combined reply -- each meal on its own line -- plus a
    single running total, rather than a separate message per meal."""
    if len(meals) == 1:
        await _log_meal_and_reply(update, chat_id, meals[0])
        return

    lines = []
    for m in meals:
        meal_id = db.add_meal(
            chat_id, m.get("meal_type"), m.get("items") or m.get("meal_items"),
            m.get("calories_low"), m.get("calories_high"), m.get("calories_estimate"),
            water_ml=m.get("water_ml"),
        )
        row = db.get_meal(chat_id, meal_id)
        items = ", ".join(row["items"]) or "meal"
        water_tag = f", +{row['water_ml']:.0f}ml water" if row.get("water_ml") else ""
        lines.append(f"{items} -- {_calorie_range(row)}{water_tag}")

    body = "\n".join(f"- {ln}" for ln in lines)
    await _reply(
        update, chat_id,
        f"Logged {len(lines)} meals:\n{body}\n\n{_daily_meal_totals_text(chat_id)}"
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
    """A photo, sent with or without a caption, logs directly -- no command
    needed. What it logs AS depends on what the photo actually shows
    (ai.extract_from_photo classifies it first, rather than assuming every
    photo is a meal):
    - real food/drink -> logged as a meal, same as before.
    - a fitness app/wearable's calorie-burned or workout-stats screen ->
      logged as a workout instead (this is the fix for a real bug: this
      case used to either get logged as a fake, nonsense meal built from the
      caption, or -- an earlier, narrower fix -- get rejected outright as
      "not food" even though it plainly was useful data, just not a meal).
    - anything else (a receipt, an unrelated photo, ...) -> not logged;
      asks what the user actually wants to do with it instead of guessing."""
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    caption = (update.message.caption or "").strip() or None
    photo = update.message.photo[-1]  # highest-resolution size Telegram offers
    tg_file = await photo.get_file()
    image_bytes = bytes(await tg_file.download_as_bytearray())
    data = ai.extract_from_photo(image_bytes, caption)
    kind = data.get("kind")
    if kind == "meal":
        await _log_meal_and_reply(update, chat_id, data)
    elif kind == "workout":
        await _log_workout_and_reply(update, chat_id, data)
    else:
        await _reply(
            update, chat_id,
            "I couldn't tell that was a food photo or a workout/fitness stats screen, so I didn't log anything. "
            "Tell me what it shows (a meal, or calories burned/a workout) and I'll log that instead."
        )


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
