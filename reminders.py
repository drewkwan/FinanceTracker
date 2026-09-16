"""
Daily recurring reminders: /addreminder, /reminders, /donereminder,
/removereminder -- standing daily habits (e.g. "take hair pills") that
resurface every day until removed, distinct from tasks.py's to-dos (which
are one-off and permanently "done" once checked off -- see db.py's
"daily reminders" section for why this is its own domain/table).

Deliberately narrower than tasks.py right now: natural language handles
ADDING one ("remind me every day to take my hair pills") and SHOWING the
list ("what are my daily reminders"), the same direct-answer discipline as
show_tasks/show_balance -- but marking one done for today or removing one
is slash-command-only (/donereminder <id>, /removereminder <id>), the same
id-based shape as /done <id>. Extending natural language to those two
would mean teaching ai.parse_message to match a reminder by id the way
"correction" already does for tasks/meals/workouts, which needs its own
recent-reminders list threaded into every parse_message call -- deferred
until it's actually needed, rather than widening that call's signature (and
every test that mocks it) for a feature not yet asked for.
"""

from telegram import Update
from telegram.ext import ContextTypes

import db
from access import _reject_if_not_allowed
from correction import LAST_CORRECTION_KEY
from formatting import _reminder_line
from replies import _reply


def _reminders_text(chat_id: int) -> str:
    """Shared by /reminders and the natural-language show_reminders intent
    -- see finance._balance_text's docstring for why. Shows every standing
    reminder, done-today ones included (tagged "[done today]") rather than
    hiding them -- so the list is a stable reference of everything
    standing, not just what's left; the morning briefing is the one place
    that filters down to only what's still pending (see morning.py)."""
    rows = db.get_active_reminders(chat_id)
    if not rows:
        return "No daily reminders set yet -- tell me something you do every day and I'll remind you."
    return "Daily reminders:\n" + "\n".join(_reminder_line(r) for r in rows)


async def _add_reminder_and_reply(update: Update, chat_id: int, description: str):
    """Shared by /addreminder and the natural-language add_reminder intent."""
    reminder_id = db.add_reminder(chat_id, description)
    row = db.get_reminder(chat_id, reminder_id)
    await _reply(
        update, chat_id,
        f"Added daily reminder: {_reminder_line(row)}. I'll bring it up in your morning briefing until you "
        f"remove it with /removereminder {row['id']}."
    )


async def addreminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    description = " ".join(context.args)
    if not description:
        await update.message.reply_text("Usage: /addreminder take hair pills")
        return
    await _add_reminder_and_reply(update, chat_id, description)


async def reminders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text(_reminders_text(chat_id))


async def donereminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /donereminder <id> (see /reminders for IDs)")
        return
    try:
        reminder_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a reminder ID. Try: /donereminder 3")
        return
    updated = db.mark_reminder_done_today(chat_id, reminder_id)
    if updated is None:
        await update.message.reply_text("Couldn't find that reminder -- run /reminders to check the ID.")
        return
    context.chat_data[LAST_CORRECTION_KEY] = {"domain": "reminder", "action": "mark_done", "reminder_id": reminder_id}
    await update.message.reply_text(f"Marked done for today: {_reminder_line(updated)}. Reply 'undo' if that's wrong.")


async def removereminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /removereminder <id> (see /reminders for IDs)")
        return
    try:
        reminder_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a reminder ID. Try: /removereminder 3")
        return
    deleted = db.delete_reminder(chat_id, reminder_id)
    if deleted is None:
        await update.message.reply_text("Couldn't find that reminder -- run /reminders to check the ID.")
        return
    context.chat_data[LAST_CORRECTION_KEY] = {"domain": "reminder", "action": "delete", "row": deleted}
    await update.message.reply_text(f"Removed: {_reminder_line(deleted)}. Reply 'undo' if that's wrong.")
