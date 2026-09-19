"""
Structured gym-exercise logging: /loglift, /recentlifts, and the
natural-language log_lift intent. Deliberately separate from workouts
(fitness.py), which stays a single free-text blob per session -- see
db.py's "lifts" module docstring for the reasoning. This is Phase A of the
coaching-engine work described in the planning session: just getting real
per-(exercise, location) data flowing into structured rows instead of
vanishing into a free-text activity string. No progression/target logic
reads this yet -- that's a later phase.
"""

from telegram import Update
from telegram.ext import ContextTypes

import ai
import db
from access import _reject_if_not_allowed
from formatting import _lift_line
from nutrition import _target_date_from_days_ago
from replies import _reply


def _recent_lifts_for_ai(chat_id: int) -> list:
    rows = db.get_recent_lifts(chat_id, limit=8)
    return [
        {"id": r["id"], "exercise": r["exercise"], "location": r["location"],
         "sets": r["sets"], "lift_date": r["lift_date"]}
        for r in rows
    ]


async def _log_lift_and_reply(update: Update, chat_id: int, item: dict, lift_date: str | None = None):
    """Shared by /loglift and the single-exercise natural-language path --
    one insert, one reply shape (see finance._balance_text's docstring for
    the same reasoning applied elsewhere)."""
    lift_id = db.add_lift(
        chat_id, item.get("exercise") or "exercise", item.get("location"), item.get("sets"),
        effort=item.get("effort"), context_notes=item.get("context_notes"), lift_date=lift_date,
    )
    row = db.get_lift(chat_id, lift_id)
    await _reply(update, chat_id, f"Logged: {_lift_line(row)}")


async def _log_lifts_and_reply(update: Update, chat_id: int, lifts: list):
    """Entry point for the natural-language log_lift intent, which can name
    several distinct exercises in one message (e.g. "pull day at wheelock:
    pull-ups 10x3, v-bar rows 35kg 8x3, lat pulldown 70kg 8x3") -- ai.py
    always returns a list ("lifts"), mirroring log_task's/add_event's own
    multi-item discipline.

    Each item carries its own "logged_days_ago" (see ai.py's logged_days_ago
    rule), converted to a real date the same deterministic way every other
    domain's does (see nutrition._target_date_from_days_ago).

    A single exercise reuses _log_lift_and_reply's exact wording/behavior
    unchanged; more than one gets ONE combined reply -- each exercise on its
    own line -- rather than a separate message per item."""
    if len(lifts) == 1:
        item = lifts[0]
        await _log_lift_and_reply(
            update, chat_id, item, lift_date=_target_date_from_days_ago(item.get("logged_days_ago"))
        )
        return

    lines = []
    for item in lifts:
        lift_date = _target_date_from_days_ago(item.get("logged_days_ago"))
        lift_id = db.add_lift(
            chat_id, item.get("exercise") or "exercise", item.get("location"), item.get("sets"),
            effort=item.get("effort"), context_notes=item.get("context_notes"), lift_date=lift_date,
        )
        row = db.get_lift(chat_id, lift_id)
        lines.append(_lift_line(row))
    await _reply(update, chat_id, "Logged:\n" + "\n".join(f"- {ln}" for ln in lines))


async def loglift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    description = " ".join(context.args)
    if not description:
        await update.message.reply_text("Usage: /loglift pull-ups 10x3 at wheelock")
        return
    data = ai.extract_lift(description)
    await _log_lift_and_reply(update, chat_id, data)


async def recentlifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    limit = 10
    if context.args:
        try:
            limit = max(1, min(50, int(context.args[0])))
        except ValueError:
            pass
    rows = db.get_recent_lifts(chat_id, limit=limit)
    if not rows:
        await update.message.reply_text("No lifts logged yet.")
        return
    await update.message.reply_text("Recent lifts:\n" + "\n".join(_lift_line(r) for r in rows))
