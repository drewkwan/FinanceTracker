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
from formatting import _lift_line, _set_str
from nutrition import _target_date_from_days_ago
from replies import _reply


def _recent_lifts_for_ai(chat_id: int) -> list:
    rows = db.get_recent_lifts(chat_id, limit=8)
    return [
        {"id": r["id"], "exercise": r["exercise"], "location": r["location"],
         "sets": r["sets"], "lift_date": r["lift_date"]}
        for r in rows
    ]


def _recent_lifts_for_narration(chat_id: int, limit: int = 40) -> list:
    """Real logged-lift rows for ai.answer_casually to ground gym-routine
    questions in (e.g. "what's my push day at Visa look like") -- the same
    "real numbers in, never guessed" discipline as day_stats/rundown,
    applied here because these questions were being answered by freely
    narrating from the loose memory-table prose blob (or conversation
    history) instead, causing exact sets/reps/weight to drift between
    successive near-identical questions in the same conversation -- a real
    observed bug. Deliberately a larger pool than _recent_lifts_for_ai's
    correction-matching 8 (narration needs enough history to actually
    answer "what does my push day look like", not just the last couple of
    entries) and fuller per-row detail (effort, context_notes) a narrated
    answer can actually use, versus just what a correction needs to
    identify one row by id."""
    rows = db.get_recent_lifts(chat_id, limit=limit)
    return [
        {"exercise": r["exercise"], "location": r["location"], "sets": r["sets"],
         "effort": r.get("effort"), "context_notes": r.get("context_notes"), "lift_date": r["lift_date"]}
        for r in rows
    ]


def _last_lift_text(chat_id: int, exercise: str, exclude_id: int) -> str | None:
    """Finds the most recent OTHER logged lift with the same exercise name
    (case-insensitive exact match), if any, within the recent pool
    db.get_recent_lifts already fetches -- real DB read, no AI involved,
    same discipline as formatting._weekly_workout_summary_text. Returns
    None (not "no prior data" text) when there's nothing to compare against,
    so a genuinely new exercise doesn't get a confusing empty comparison
    line. Deliberately just the sets, not the full _lift_line -- the
    exercise name and location are already in the line just above this
    one, repeating them would be noise."""
    target = (exercise or "").strip().lower()
    if not target:
        return None
    for row in db.get_recent_lifts(chat_id, limit=30):
        if row["id"] == exclude_id:
            continue
        if (row.get("exercise") or "").strip().lower() == target:
            sets_str = ", ".join(_set_str(s) for s in (row.get("sets") or [])) or "no sets recorded"
            return f"Last time ({row['lift_date']}): {sets_str}"
    return None


async def _log_lift_and_reply(update: Update, chat_id: int, item: dict, lift_date: str | None = None):
    """Shared by /loglift and the single-exercise natural-language path --
    one insert, one reply shape (see finance._balance_text's docstring for
    the same reasoning applied elsewhere). Also shows a same-exercise
    comparison against the most recent prior time this exercise was logged,
    when there is one (see _last_lift_text) -- the kind of thing a real
    training partner would actually notice and mention, not just a bare
    "logged" confirmation."""
    lift_id = db.add_lift(
        chat_id, item.get("exercise") or "exercise", item.get("location"), item.get("sets"),
        effort=item.get("effort"), context_notes=item.get("context_notes"), lift_date=lift_date,
    )
    row = db.get_lift(chat_id, lift_id)
    reply = f"Logged: {_lift_line(row)}"
    last = _last_lift_text(chat_id, row["exercise"], exclude_id=row["id"])
    if last:
        reply += f"\n{last}"
    await _reply(update, chat_id, reply)


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
