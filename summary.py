"""
AI-written spending breakdown: /summary [today|week|month]. Real, code-
computed category totals, day-of-week averages, and budget adherence are
handed to Claude only to narrate -- if that synthesis call fails, the
fallback below renders the same real numbers directly rather than staying
silent (see rundown._rundown_fallback_text for the identical discipline
applied to /rundown).
"""

import logging
from datetime import date, timedelta

from telegram import Update
from telegram.ext import ContextTypes

import ai
import config
import db
import trends
from access import _reject_if_not_allowed
from formatting import _money
from replies import _reply

logger = logging.getLogger(__name__)

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# How many equal-length periods immediately before the current one to
# average for a "is this normal for you" baseline per category -- e.g. for
# a monthly /summary, the trailing 6 months. Deliberately a period-relative
# window (not a fixed calendar length) so the same logic works whether the
# request is for today, a week, or a month: the baseline is always "N of
# these", not "N months" scaled awkwardly onto a week. Chosen to include
# the immediately-prior period (the same one shown separately as
# previous_period) plus enough history either side of it to smooth out one
# unusually quiet or busy period.
BASELINE_LOOKBACK_PERIODS = 6


def _category_insights_for_period(chat_id: int, current_totals: list[dict], current_start: date,
                                    length: int) -> list[dict]:
    """A category total alone can't tell a one-off big-ticket purchase from a
    genuine behavioral spike -- both need a real historical baseline,
    computed here rather than left for the model to guess at, same "real
    numbers in" discipline as everything else in this file. `current_totals`
    is passed in (not re-queried) since /summary's own caller already has it
    for the empty-period early-return and the fallback text.

    This is deliberately its own function, not inlined into summary() --
    insights._detect_spending_insights reuses this exact same baseline math
    for its own "is this category running hot right now" check, and it
    would be a real risk to have two separate implementations of the same
    "what's typical for you" computation quietly drift apart over time."""
    baseline_start = current_start - timedelta(days=BASELINE_LOOKBACK_PERIODS * length)
    baseline_totals = db.get_category_totals(chat_id, baseline_start.isoformat(), current_start.isoformat())
    typical_per_period = {r["category"]: r["total"] / BASELINE_LOOKBACK_PERIODS for r in baseline_totals}
    return [
        {
            "category": r["category"],
            "total": r["total"],
            "n": r["n"],
            # Average total for this category over the BASELINE_LOOKBACK_PERIODS
            # periods immediately before this one -- "what you normally spend
            # here in a period this length." None (not 0) when there's no
            # history at all for this category, so the model doesn't read "0"
            # as "you never spend on this" versus "no data yet."
            "typical_per_period": round(typical_per_period.get(r["category"], 0.0), 2)
            if r["category"] in typical_per_period else None,
            "vs_typical_ratio": (
                round(r["total"] / typical_per_period[r["category"]], 1)
                if typical_per_period.get(r["category"]) else None
            ),
        }
        for r in current_totals
    ]


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    chat_id = update.effective_chat.id
    period_arg = context.args[0] if context.args else "week"
    # db.today_str() -- NOT date.today() -- see fx._now_local_date's
    # docstring: date.today() is the server's/OS date (UTC on Railway),
    # which lags Asia/Singapore's actual calendar date by up to 8 hours
    # after local midnight, so "/summary today" run in that window used to
    # silently show yesterday's totals under the label "today".
    bounds = trends.period_bounds(period_arg, date.fromisoformat(db.today_str()))
    period = bounds["period"]
    length = bounds["length"]
    today = bounds["today"]
    tomorrow = bounds["tomorrow"]
    current_start = bounds["current_start"]
    current_end = bounds["current_end"]
    prev_start = bounds["prev_start"]
    prev_end = bounds["prev_end"]

    current_totals = db.get_category_totals(chat_id, current_start.isoformat(), current_end.isoformat())
    if not current_totals:
        await update.message.reply_text(f"No spending logged in the last {period}.")
        return
    previous_totals = db.get_category_totals(chat_id, prev_start.isoformat(), prev_end.isoformat())

    current_total = sum(r["total"] for r in current_totals)
    previous_total = sum(r["total"] for r in previous_totals)

    user = db.get_or_create_user(chat_id)

    # See _category_insights_for_period's docstring for why this is its own
    # function rather than computed inline here.
    category_insights = _category_insights_for_period(chat_id, current_totals, current_start, length)

    top_transactions = [
        {
            "amount": round(t["amount_base"], 2),
            "description": t["description"],
            "category": t["category"],
            "date": t["expense_date"],
        }
        for t in db.get_largest_expenses(chat_id, current_start.isoformat(), current_end.isoformat(), limit=5)
    ]

    payload = {
        "base_currency": config.BASE_CURRENCY,
        "current_period": {"days": length, "total": round(current_total, 2), "by_category": current_totals},
        "previous_period": {"days": length, "total": round(previous_total, 2), "by_category": previous_totals},
        "streak": {"current": user["current_streak"], "best": user["best_streak"]},
        "month_to_date": db.get_month_to_date_total(chat_id),
        # New for the advisor-style insights upgrade (see ai.py's
        # answer_with_trends): per-category totals paired with a real
        # historical baseline and ratio, plus the actual largest individual
        # transactions this period -- so the model can distinguish "a category
        # spiked because of one big-ticket item" from "steady overspend across
        # many small purchases," and a one-off occasion from a genuine pattern,
        # instead of just reading off category totals.
        "category_insights": category_insights,
        "top_transactions": top_transactions,
    }

    # Day-of-week pattern + budget adherence only make sense with more than a single day of history.
    if period != "today":
        lookback_start = today - timedelta(days=56)  # 8 weeks, for a stable weekday average
        daily = db.get_daily_totals(chat_id, lookback_start.isoformat(), tomorrow.isoformat())
        dow_totals = [0.0] * 7
        dow_counts = [0] * 7
        d = lookback_start
        while d < tomorrow:
            dow = d.weekday()
            dow_totals[dow] += daily.get(d.isoformat(), 0.0)
            dow_counts[dow] += 1
            d += timedelta(days=1)
        payload["avg_spend_by_weekday"] = {
            WEEKDAY_NAMES[i]: round(dow_totals[i] / dow_counts[i], 2)
            for i in range(7) if dow_counts[i] > 0
        }

        period_daily = db.get_daily_totals(chat_id, current_start.isoformat(), current_end.isoformat())
        days_under = sum(
            1 for i in range(length)
            if period_daily.get((current_start + timedelta(days=i)).isoformat(), 0.0) <= user["daily_target"]
        )
        payload["budget_adherence"] = {
            "days_under_target": days_under,
            "days_in_period": length,
            "daily_target": user["daily_target"],
        }

    try:
        text = ai.answer_with_trends(period, payload)
    except Exception:
        logger.exception("AI trend summary failed, falling back to raw breakdown")
        lines = [f"{r['category']}: {_money(r['total'])} ({r['n']}x)" for r in current_totals]
        trend_line = ""
        if previous_total:
            pct = (current_total - previous_total) / previous_total * 100
            direction = "Up" if pct > 0 else "Down"
            trend_line = f"\n{direction} {abs(pct):.0f}% vs the prior {period}."
        text = (f"Spending -- last {period}:\n" + "\n".join(lines) +
                f"\n\nTotal: {_money(current_total)}{trend_line}")
    # narrate=False -- ai.answer_with_trends (and the raw fallback above) are
    # already the final, complete reply, the same "don't narrate a
    # narration" discipline as rundown/day_stats/casual (see replies._reply's
    # docstring). This used to call update.message.reply_text directly,
    # which meant /summary was the one place in the whole bot that skipped
    # Morrow's companion voice entirely -- a real inconsistency, not a
    # deliberate exemption like rundown/day_stats/casual's narrate=False is.
    await _reply(update, chat_id, text, narrate=False)
