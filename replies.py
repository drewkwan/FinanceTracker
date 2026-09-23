"""
The one place a Telegram reply is actually sent for the free-text chat
surface, plus the budget-threshold nudge that piggybacks on it. Also where
EVERY reply on that surface -- not just the 'casual' intent -- gets run
through Morrow's companion voice (ai.narrate_reply) before it goes out, so
the bot feels like a personal companion at all times, not just during
open-ended chat (see _narrate_text's docstring for the real user complaint
this closes: logging still read as a flat receipt printer even after
answer_casually shipped, because that call only ever fired for pure
chit-chat, never for the confirmations that make up most of what Morrow
actually sends).
"""

import logging

from telegram import Update

import config
import db
from formatting import _status_text

logger = logging.getLogger(__name__)

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


async def _narrate_text(chat_id: int, text: str) -> str:
    """Runs `text` -- an already-correct, deterministically-computed reply
    -- through ai.narrate_reply so it comes out sounding like Morrow's own
    companion voice instead of a flat confirmation string, fed the same
    grounding context answer_casually uses (recent conversation history,
    durable memory, today's real numbers, recent logged lifts) so it reads
    as a continuation of the actual conversation, not a stateless rewrite.

    The imports below are deliberately local, not at module level: replies.py
    is a leaf module several domain modules (lifts, fitness, correction, ...)
    import FROM (see this module's own docstring), so importing any of them
    back at load time would be circular -- by the time this function actually
    runs, every module is already loaded, so a local import works fine and
    costs nothing extra per call.

    Never raises -- falls back to `text` unchanged on ANY failure (a
    narration API hiccup must never turn into a lost, garbled, or delayed
    reply), the same "never go silent" discipline as every other narration
    call in this codebase."""
    import ai
    from lifts import _recent_lifts_for_narration
    from memory import _memory_for_ai
    from rundown import _day_stats_payload
    try:
        recent_messages = [
            {"role": r["role"], "content": r["content"]}
            for r in db.get_recent_messages(chat_id, limit=30)  # mirrors handlers.RECENT_MESSAGES_FOR_AI
        ]
        memory_list = _memory_for_ai(chat_id)
        today_snapshot = {
            "balance": db.get_status(chat_id),
            "today": _day_stats_payload(chat_id, db.today_str()),
        }
        recent_lifts = _recent_lifts_for_narration(chat_id)
        return ai.narrate_reply(text, recent_messages, memory_list, today_snapshot, recent_lifts)
    except Exception:
        logger.exception("narrate_reply failed, sending the deterministic reply unchanged")
        return text


async def _reply(update: Update, chat_id: int, text: str, *, narrate: bool = True):
    """Send a Telegram reply AND persist it to the rolling conversation
    history (db.add_message) -- used throughout the free-text (handle_text)
    call path, and by every domain module's command AND natural-language
    handlers that share a reply helper (e.g. lifts._log_lift_and_reply),
    so both surfaces get the same companion treatment.

    narrate=True (the default) runs `text` through _narrate_text -- Morrow's
    companion voice -- before sending, so a plain "Logged: pull-ups 10x3"
    reads like an actual companion responding, not a receipt. Pass
    narrate=False for content that's ALREADY been through its own dedicated
    narration call (ai.answer_casually, answer_with_rundown,
    answer_with_day_stats) -- routing that back through a second narration
    pass would be wasteful and risks quietly corrupting numbers a first,
    more carefully-grounded pass already got right (see handlers.py's
    'casual'/'rundown'/'day_stats' intent branches)."""
    if narrate:
        text = await _narrate_text(chat_id, text)
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
