"""
The one place a Telegram reply is actually sent for the free-text chat
surface, plus the budget-threshold nudge that piggybacks on it.
"""

from telegram import Update

import config
import db
from formatting import _status_text


async def _reply(update: Update, chat_id: int, text: str):
    """Send a Telegram reply AND persist it to the rolling conversation
    history (db.add_message) -- used throughout the free-text (handle_text)
    call path so Morrow's own turns land in short-term memory alongside the
    user's, not just the structured domain tables. Slash commands don't go
    through this yet (see handlers.py's module notes) -- this is scoped to
    the open-ended chat surface first."""
    await update.message.reply_text(text)
    db.add_message(chat_id, "morrow", text)


async def _send_alert_if_needed(update: Update, chat_id: int):
    if db.maybe_alert(chat_id):
        status = db.get_status(chat_id)
        pct = int(config.BUDGET_ALERT_THRESHOLD * 100)
        await _reply(
            update, chat_id,
            f"Heads up: you've crossed {pct}% of today's available budget.\n\n{_status_text(status)}"
        )
