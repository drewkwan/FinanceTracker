"""
Morning briefing: a proactive daily digest -- today's budget, today's/
overdue to-dos, today's still-pending daily reminders, what's coming up on
the schedule this week, a brief look back at yesterday, and any notable
cross-domain patterns insights.py detected (a spending category running
hot, a vitals trend, a lift that's overdue or hit a new top set) -- pushed
automatically once a day (see app.py's job_queue.run_daily wiring), plus
/morning to preview the same content any time.

Insights ride along in this same briefing rather than sending their own
separate proactive messages -- see insights.py's module docstring for the
detection side, and morning_briefing_tick's docstring for why the dedup
bookkeeping only happens on the real automatic send, never on the /morning
preview.

Deliberately forward-looking, unlike rundown.py's 7-day retrospective: the
point of a MORNING briefing is "what needs my attention today", not a
week's trend. Same "never let the model guess a number" discipline as
rundown.py and finance._balance_text -- every figure here is computed
deterministically in Python; _morning_briefing_text builds the real,
final content with no AI involved at all, so the one message that's
supposed to show up reliably every single morning can never be delayed or
broken by an API hiccup mid-computation.

It IS, however, run through Morrow's companion voice before it's actually
sent -- see replies._reply/_send_proactive's narrate=True default and
ai.narrate_reply's docstring. That's a pure restyle of this already-correct
text (never a re-synthesis), with the same fall-back-to-the-original-text
safety net as every other narrated reply, so the briefing still can't be
lost or delayed by an API hiccup -- narration failing just means it goes
out in its plain deterministic form instead.
"""

import logging
from datetime import date, timedelta

from telegram import Update
from telegram.ext import ContextTypes

import db
import insights
from access import _reject_if_not_allowed
from formatting import _event_line, _money, _reminder_line, _task_line
from replies import _reply, _send_proactive

# How many days ahead "Coming up" looks -- a focused near-term window, the
# same reasoning as due_today_or_overdue only surfacing today/overdue tasks:
# a morning briefing is about what needs attention soon, not a full schedule
# dump (that's /events, on request). A week matches the workout-planning use
# case this was built for (a week's plan committed as flat dated events).
EVENTS_LOOKAHEAD_DAYS = 7

logger = logging.getLogger(__name__)


def _morning_briefing_payload(chat_id: int) -> dict:
    """Real, deterministically-computed figures for the morning briefing.
    db.get_status already runs ensure_rollover, so balance/streak are
    always fresh by the time this reads them -- no separate rollover step
    needed here."""
    today_str = db.today_str()
    yesterday_str = (date.fromisoformat(today_str) - timedelta(days=1)).isoformat()

    status = db.get_status(chat_id)
    # Only tasks due today or already overdue -- an undated to-do, or one
    # due later this week, isn't something that needs attention THIS
    # morning specifically; /tasks still shows the full list on request.
    due_today_or_overdue = [
        t for t in db.get_open_tasks(chat_id) if t["due_at"] and t["due_at"][:10] <= today_str
    ]
    # Only the ones NOT already checked off today -- see db.py's "daily
    # reminders" section for why nothing has to reset this at midnight: a
    # reminder done yesterday (or never) naturally shows as pending again
    # the moment today_str() advances, with no separate rollover job.
    reminders_pending = [
        r for r in db.get_active_reminders(chat_id) if r["last_done_date"] != today_str
    ]

    # Upcoming events within the lookahead window only -- see
    # EVENTS_LOOKAHEAD_DAYS' comment for why this isn't the full /events list.
    lookahead_end = (date.fromisoformat(today_str) + timedelta(days=EVENTS_LOOKAHEAD_DAYS)).isoformat()
    events_upcoming = [
        e for e in db.get_upcoming_events(chat_id, from_date=today_str) if e["event_date"] <= lookahead_end
    ]

    y_meals = db.get_meals_in_range(chat_id, yesterday_str, today_str)
    y_workouts = db.get_workouts_in_range(chat_id, yesterday_str, today_str)
    y_vitals = db.get_vitals_in_range(chat_id, yesterday_str, today_str)

    return {
        "today_str": today_str,
        "status": status,
        "due_today_or_overdue": due_today_or_overdue,
        "reminders_pending": reminders_pending,
        "events_upcoming": events_upcoming,
        "yesterday": {
            "calories": sum(m["calories_estimate"] or 0 for m in y_meals) if y_meals else None,
            "water_ml": sum(m["water_ml"] or 0 for m in y_meals) if y_meals else None,
            "workout_activities": [w["activity"] for w in y_workouts if w["activity"]],
            "vitals_checkins": len(y_vitals),
        },
        # Real, already-throttled/deduped observations across every domain
        # (spending, vitals, lifts) -- see insights.py's module docstring.
        # Deliberately computed here (in the payload, alongside everything
        # else) rather than fetched separately in morning_briefing_tick, so
        # /morning's on-demand preview shows exactly the same content the
        # automatic push would -- same discipline as every other field here.
        "insights": insights.surfaceable_insights(chat_id, today_str),
    }


def _morning_briefing_text(payload: dict) -> str:
    """Plain deterministic rendering -- see the module docstring for why
    there's no AI call in this one, unlike /rundown's narrated version."""
    status = payload["status"]
    lines = [
        "Good morning! Here's where things stand:",
        "",
        f"Today's target: {_money(status['daily_target'])}",
        f"Rolled-over balance: {_money(status['balance'])}",
        f"Available today: {_money(status['available_today'])}",
    ]
    if status["current_streak"] > 0:
        lines.append(f"Streak: {status['current_streak']} day(s) within budget (best: {status['best_streak']})")

    due = payload["due_today_or_overdue"]
    if due:
        lines.append("")
        lines.append("Due today or overdue:")
        for t in due:
            tag = " [overdue]" if t["due_at"][:10] < payload["today_str"] else ""
            lines.append(f"{_task_line(t)}{tag}")

    reminders_pending = payload["reminders_pending"]
    if reminders_pending:
        lines.append("")
        lines.append("Daily reminders still to do today:")
        for r in reminders_pending:
            lines.append(_reminder_line(r))

    events_upcoming = payload["events_upcoming"]
    if events_upcoming:
        lines.append("")
        lines.append("Coming up:")
        for e in events_upcoming:
            lines.append(_event_line(e))

    y = payload["yesterday"]
    if y["calories"] or y["workout_activities"] or y["vitals_checkins"]:
        bits = []
        if y["calories"]:
            water_tag = f", {y['water_ml']:.0f}ml water" if y["water_ml"] else ""
            bits.append(f"~{y['calories']:.0f} kcal eaten{water_tag}")
        if y["workout_activities"]:
            bits.append(f"workout: {', '.join(y['workout_activities'])}")
        if y["vitals_checkins"]:
            plural = "" if y["vitals_checkins"] == 1 else "s"
            bits.append(f"{y['vitals_checkins']} vitals check-in{plural}")
        lines.append("")
        lines.append("Yesterday: " + "; ".join(bits))

    notable = payload.get("insights") or []
    if notable:
        lines.append("")
        lines.append("Noticed:")
        for insight in notable:
            lines.append(f"- {insight['headline']}")

    return "\n".join(lines)


async def morning_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual preview of the morning briefing -- exactly the same content
    the automatic daily push sends (see morning_briefing_tick), so there's
    no need to wait until morning to check it works, or to see today's
    digest again later in the day."""
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    await _reply(update, chat_id, _morning_briefing_text(_morning_briefing_payload(chat_id)))


async def morning_briefing_tick(context: ContextTypes.DEFAULT_TYPE):
    """Runs once a day at a fixed local time (see app.py's
    job_queue.run_daily wiring) and pushes the briefing to every known chat
    -- same all-chats loop and same per-chat try/except as
    app.rollover_tick, so one chat's failure (e.g. they blocked the bot)
    never blocks the briefing going out to everyone else.

    Any insights included get recorded via db.record_insight_sent right
    here, AFTER a successful send -- deliberately not inside
    _morning_briefing_payload/morning_cmd, so previewing today's briefing
    with /morning can never itself burn an insight's dedup window without
    it ever having actually gone out proactively."""
    for chat_id in db.get_all_chat_ids():
        try:
            payload = _morning_briefing_payload(chat_id)
            text = _morning_briefing_text(payload)
            await _send_proactive(context, chat_id, text)
            for insight in payload.get("insights") or []:
                db.record_insight_sent(chat_id, insight["dedup_key"])
        except Exception:
            logger.exception("Failed to send morning briefing to chat %s", chat_id)
