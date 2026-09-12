"""
Cross-domain synthesis: /rundown. Real, deterministically-computed figures
across all four domains (balance, meals, workouts, vitals) for the trailing
window -- handed to Claude only to narrate, never to invent a number. Same
"never let the model guess a number" discipline as finance._balance_text.
"""

import logging
from datetime import date, timedelta

from telegram import Update
from telegram.ext import ContextTypes

import ai
import db
from access import _reject_if_not_allowed

logger = logging.getLogger(__name__)

RUNDOWN_WINDOW_DAYS = 7


def _rundown_payload(chat_id: int) -> dict:
    """Real, deterministically-computed figures across all four domains for
    the last RUNDOWN_WINDOW_DAYS (inclusive of today) -- fed to
    ai.answer_with_rundown for synthesis. Shared by the natural-language
    'rundown' intent and /rundown, same discipline as finance._balance_text /
    finance._recent_text: one implementation, not two."""
    today = date.fromisoformat(db.today_str())
    window_start = today - timedelta(days=RUNDOWN_WINDOW_DAYS - 1)
    tomorrow = today + timedelta(days=1)

    status = db.get_status(chat_id)

    meals = db.get_meals_in_range(chat_id, window_start.isoformat(), tomorrow.isoformat())
    total_calories = sum(m["calories_estimate"] or 0 for m in meals)
    total_water = sum(m["water_ml"] or 0 for m in meals)

    workouts = db.get_workouts_in_range(chat_id, window_start.isoformat(), tomorrow.isoformat())

    vitals = db.get_vitals_in_range(chat_id, window_start.isoformat(), tomorrow.isoformat())
    weights = [v["weight_kg"] for v in vitals if v["weight_kg"] is not None]
    sleep_hours = [v["sleep_hours"] for v in vitals if v["sleep_hours"] is not None]
    knee_pain = [v["knee_pain"] for v in vitals if v["knee_pain"] is not None]

    return {
        "window_days": RUNDOWN_WINDOW_DAYS,
        "balance": {
            "available_today": round(status["available_today"], 2),
            "spent_today": round(status["spent_today"], 2),
            "daily_target": status["daily_target"],
            "current_streak": status["current_streak"],
        },
        "meals": {
            "count": len(meals),
            "total_calories_estimate": total_calories if meals else None,
            "total_water_ml": total_water if meals else None,
        },
        "workouts": {
            "count": len(workouts),
            "activities": [w["activity"] for w in workouts if w["activity"]],
        },
        "vitals": {
            "checkins": len(vitals),
            "latest_weight_kg": weights[-1] if weights else None,
            "weight_change_kg": round(weights[-1] - weights[0], 2) if len(weights) >= 2 else None,
            "avg_sleep_hours": round(sum(sleep_hours) / len(sleep_hours), 2) if sleep_hours else None,
            "avg_knee_pain": round(sum(knee_pain) / len(knee_pain), 2) if knee_pain else None,
        },
    }


def _rundown_fallback_text(payload: dict) -> str:
    """Raw, deterministic rendering used only if the Claude synthesis call
    itself fails -- never silent, same discipline as summary.summary's
    fallback. Skips a section entirely when there's nothing in it, same as
    the narrative prompt is told to."""
    lines = [f"Last {payload['window_days']} days:"]
    bal = payload["balance"]
    lines.append(
        f"Balance: {bal['available_today']} available today (streak {bal['current_streak']}d)"
    )
    meals = payload["meals"]
    if meals["count"]:
        lines.append(f"Meals: {meals['count']} logged, ~{meals['total_calories_estimate']} cal")
    workouts = payload["workouts"]
    if workouts["count"]:
        activities = ", ".join(workouts["activities"]) or "unspecified"
        lines.append(f"Workouts: {workouts['count']} ({activities})")
    vitals = payload["vitals"]
    if vitals["checkins"]:
        weight_line = f", latest weight {vitals['latest_weight_kg']}kg" if vitals["latest_weight_kg"] else ""
        lines.append(f"Vitals: {vitals['checkins']} check-in(s){weight_line}")
    return "\n".join(lines)


async def _rundown_reply_text(chat_id: int) -> str:
    """Shared by /rundown and the natural-language 'rundown' intent, same
    one-implementation discipline as finance._balance_text/_recent_text."""
    payload = _rundown_payload(chat_id)
    try:
        return ai.answer_with_rundown(payload)
    except Exception:
        logger.exception("AI rundown synthesis failed, falling back to raw breakdown")
        return _rundown_fallback_text(payload)


async def rundown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text(await _rundown_reply_text(chat_id))
