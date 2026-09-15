"""
The one place a Telegram reply is actually sent for the free-text chat
surface, plus the budget-threshold nudge that piggybacks on it.
"""

from telegram import Update

import config
import db
from formatting import _status_text

# The chat_data key holding a not-yet-resolved clarification's original
# context, shared between handlers.py (the free-text clarification loop --
# ai.parse_message asked a follow-up question, the next message answers it)
# and nutrition.py (a photo whose caption seemed to name a second, different
# food -- see ai.extract_from_photo's "caption_extra_item" -- asks the same
# way and lets the SAME loop resolve the answer). Lives here, a leaf module
# with no domain imports, specifically so both of those can import it
# without a circular import (handlers.py already imports from nutrition.py).
PENDING_KEY = "pending_expense"

# A second, narrower pending-state key: a duplicate-looking workout (see
# fitness._find_matching_calories_burned/_ask_about_duplicate_workout) needs
# a yes/no answer, not a free-text re-parse -- ai.parse_message's log_workout
# shape has nowhere to carry calories_burned (only a typed /logworkout ever
# hits that path, which rarely reports one), so reusing PENDING_KEY's
# merge-and-reparse loop would silently drop the exact number the photo
# already gave us. handlers.handle_text checks this key FIRST, ahead of the
# general PENDING_KEY handling, and resolves it deterministically from the
# already-parsed photo data stored here -- no AI re-parse involved.
PENDING_DUPLICATE_WORKOUT_KEY = "pending_duplicate_workout"


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
