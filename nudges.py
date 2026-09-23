"""
Proactive evening nudge: a gentle, once-a-day check-in if literally nothing
has been logged (no meal, no workout, no vitals check-in) by a fixed local
evening time -- see app.py's job_queue.run_daily wiring, config.py's
EVENING_NUDGE_HOUR/MINUTE.

Deliberately conservative -- this is the "Moderate" proactivity slice, not
"Aggressive coach mode": it only fires on a genuinely QUIET day (nothing at
all logged), never for a partial day (e.g. a workout logged but no meals
yet), since a partial day is completely normal and nagging about it would
just be more of the annoying-unprompted-noise problem this is supposed to
fix, not less. Reuses rundown._day_stats_payload's real counts for "today"
rather than a second, separately-computed notion of "empty" -- one source
of truth for what's logged today, same reasoning as handlers._casual_reply_text
reusing it for today_snapshot.

Sent via replies._send_proactive, not a bare context.bot.send_message, so
this also gets Morrow's companion voice (ai.narrate_reply) and lands in the
rolling conversation history like every other reply -- see
morning.py's module docstring for the same reasoning applied to the
morning briefing.
"""

import logging

from telegram.ext import ContextTypes

import db
from replies import _send_proactive
from rundown import _day_stats_payload

logger = logging.getLogger(__name__)


async def evening_nudge_tick(context: ContextTypes.DEFAULT_TYPE):
    """Runs once a day at a fixed local time. Same all-chats loop and
    same per-chat try/except as morning.morning_briefing_tick/
    app.rollover_tick, so one chat's failure never blocks the nudge going
    out to everyone else."""
    today = db.today_str()
    for chat_id in db.get_all_chat_ids():
        try:
            payload = _day_stats_payload(chat_id, today)
            nothing_logged = (
                payload["meals"]["count"] == 0
                and payload["workouts"]["count"] == 0
                and payload["vitals"] is None
            )
            if not nothing_logged:
                continue
            await _send_proactive(
                context, chat_id,
                "Quiet day so far -- nothing logged yet today. All good, or just haven't had a "
                "chance? No pressure either way, just checking in."
            )
        except Exception:
            logger.exception("Failed to send evening nudge to chat %s", chat_id)
