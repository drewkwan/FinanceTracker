"""
Durable memory: /memory, /forget. Standing goals, plans, and preferences
the user explicitly asks Morrow to remember -- see db.py's module
docstring for why this is a separate table from the rolling conversation
history, not the same thing.
"""

from telegram import Update
from telegram.ext import ContextTypes

import db
from access import _reject_if_not_allowed
from correction import LAST_CORRECTION_KEY
from formatting import _memory_line


def _memory_for_ai(chat_id: int) -> list:
    rows = db.get_memory_list(chat_id)
    return [{"label": r["label"], "category": r["category"], "content": r["content"]} for r in rows]


def _memory_text(chat_id: int) -> str:
    rows = db.get_memory_list(chat_id)
    if not rows:
        return "I don't have anything saved yet -- tell me something to remember and I'll hold onto it."
    return "What I remember:\n" + "\n".join(_memory_line(r) for r in rows)


async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Explicit transparency command mirroring /recentmeals etc. -- lets the
    user check exactly what's saved without needing to ask conversationally."""
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text(_memory_text(chat_id))


async def forget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    label = " ".join(context.args)
    if not label:
        await update.message.reply_text("Usage: /forget <label> (see /memory for the exact labels)")
        return
    deleted = db.delete_memory_by_label(chat_id, label)
    if not deleted:
        await update.message.reply_text(f"Nothing saved under \"{label}\" -- run /memory to see the list.")
        return
    context.chat_data[LAST_CORRECTION_KEY] = {"domain": "memory", "action": "delete", "row": deleted}
    await update.message.reply_text(f"Forgot \"{deleted['label']}\". Reply 'undo' if that's wrong.")
