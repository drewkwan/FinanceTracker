"""
Proactive pattern detection, across every domain, independent of where it's
eventually shown. This is "coaching-engine Phase B" generalized: instead of
lift progression getting its own bespoke feature, it's one detector among
several sharing the same shape, dedup mechanism, and delivery hookup (see
morning.py's use of surfaceable_insights).

Every detector here is a pure function -- real DB reads only, no side
effects, no Telegram, no AI -- so each one is independently testable the
same way _rundown_payload/_vitals_trend_text/_last_lift_text already are.
The spending detector specifically reuses summary._category_insights_for_period
rather than re-deriving the same "what's typical for you" baseline math --
see that function's own docstring for why having two implementations of it
would be a real risk.

An Insight is a plain dict (matching every other row in this codebase --
meals/workouts/lifts/vitals are all plain dicts too, never wrapped in a
class):
    {
        "domain": "expense" | "vitals" | "lift",
        "kind": "category_spike" | "weight_trend" | "sleep_drop" | "progression" | "stale",
        "headline": short, human-readable, built from real numbers only,
        "data": the raw numbers behind the headline, for anything downstream
                that wants them without re-deriving,
        "dedup_key": stable string identifying "this specific observation",
                     used by db.was_insight_sent_recently to avoid repeating
                     the same thing every single day -- see db.py's
                     "proactive insight dedup" section for why that table
                     exists: nothing about a detector's underlying condition
                     resets day to day on its own, so without a dedup table
                     an unchanged spike/trend/stale-exercise would resurface
                     in every morning briefing, not just the first one.
    }

collect_insights() is pure aggregation (no filtering, no dedup, no
delivery) -- keep it that way so it stays trivially testable. Any policy
about which insights actually get shown, and how often, belongs in
surfaceable_insights() instead.
"""

from datetime import date, timedelta
import re

import db
import summary
import trends
from formatting import _money

# ---------- tunable thresholds -- starting points, not final numbers ----------

# A category is "notable" if it's running at least this many times its own
# historical per-period average AND has spent at least this much in
# absolute terms -- the absolute floor exists so a $12 category reading "3x
# normal" doesn't fire just because $4 used to be its typical total.
SPENDING_SPIKE_RATIO = 1.5
SPENDING_SPIKE_MIN_ABSOLUTE = 50.0

# How many trailing days count as "this week" for a vitals trend, and how
# big a real move has to be before it's worth mentioning.
VITALS_TREND_WINDOW_DAYS = 7
VITALS_WEIGHT_TREND_KG = 1.0
VITALS_SLEEP_DROP_HOURS = 1.0

# A lift needs real history before "you're overdue" means anything -- see
# _detect_lift_insights' docstring for why this is a per-exercise computed
# cadence, not a fixed number of days.
LIFT_STALE_MIN_SESSIONS = 4
LIFT_STALE_MULTIPLIER = 1.5
LIFT_STALE_MIN_DAYS = 5

# How long a given dedup_key stays suppressed after being surfaced once, and
# the hard cap on how many insights ride along in a single briefing so this
# never turns into a report.
INSIGHT_DEDUP_WINDOW_DAYS = 7
MAX_INSIGHTS_PER_BRIEFING = 2

_LOAD_KG_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*kg\s*$", re.IGNORECASE)


def _detect_spending_insights(chat_id: int, today_str: str) -> list[dict]:
    """Reuses summary._category_insights_for_period's exact baseline math
    (vs_typical_ratio) for the current calendar week -- this is genuinely
    the same "is this normal for you" question /summary already answers on
    request, just asked proactively instead of waiting to be asked."""
    bounds = trends.period_bounds("week", date.fromisoformat(today_str))
    current_totals = db.get_category_totals(
        chat_id, bounds["current_start"].isoformat(), bounds["current_end"].isoformat()
    )
    if not current_totals:
        return []
    category_insights = summary._category_insights_for_period(
        chat_id, current_totals, bounds["current_start"], bounds["length"]
    )
    out = []
    for c in category_insights:
        ratio = c["vs_typical_ratio"]
        if ratio and ratio >= SPENDING_SPIKE_RATIO and c["total"] >= SPENDING_SPIKE_MIN_ABSOLUTE:
            out.append({
                "domain": "expense",
                "kind": "category_spike",
                "headline": f"{c['category']} is {ratio}x your usual this week "
                            f"({_money(c['total'])} vs ~{_money(c['typical_per_period'])} typical)",
                "data": c,
                "dedup_key": f"expense:category_spike:{c['category']}",
            })
    return out


def _detect_vitals_insights(chat_id: int, today_str: str) -> list[dict]:
    """Generalizes vitals._vitals_trend_text, which only ever compares
    against the single most recent prior check-in, into a real rolling-
    window trend -- a sustained move across the week, not just one delta.
    Requires actual history on both sides of the comparison (never guesses
    at a "typical" sleep amount) -- returns nothing when there isn't
    enough data to say something real."""
    today = date.fromisoformat(today_str)
    window_start = (today - timedelta(days=VITALS_TREND_WINDOW_DAYS - 1)).isoformat()
    window_end = (today + timedelta(days=1)).isoformat()
    rows = db.get_vitals_in_range(chat_id, window_start, window_end)  # oldest-first
    out = []

    weights = [r["weight_kg"] for r in rows if r.get("weight_kg") is not None]
    if len(weights) >= 2:
        delta = weights[-1] - weights[0]
        if abs(delta) >= VITALS_WEIGHT_TREND_KG:
            direction = "down" if delta < 0 else "up"
            out.append({
                "domain": "vitals",
                "kind": "weight_trend",
                "headline": f"Weight is {direction} {abs(delta):.1f}kg over the last "
                            f"{VITALS_TREND_WINDOW_DAYS} days ({weights[0]:.1f}kg -> {weights[-1]:.1f}kg)",
                "data": {"start_kg": weights[0], "end_kg": weights[-1], "delta_kg": round(delta, 1)},
                "dedup_key": "vitals:weight_trend",
            })

    sleeps = [r["sleep_hours"] for r in rows if r.get("sleep_hours") is not None]
    if len(sleeps) >= 3:  # a couple of nights isn't a real "average" yet
        avg_sleep = sum(sleeps) / len(sleeps)
        prior_start = (date.fromisoformat(window_start) - timedelta(days=VITALS_TREND_WINDOW_DAYS)).isoformat()
        prior_sleeps = [
            r["sleep_hours"] for r in db.get_vitals_in_range(chat_id, prior_start, window_start)
            if r.get("sleep_hours") is not None
        ]
        if len(prior_sleeps) >= 3:
            prior_avg = sum(prior_sleeps) / len(prior_sleeps)
            if prior_avg - avg_sleep >= VITALS_SLEEP_DROP_HOURS:
                out.append({
                    "domain": "vitals",
                    "kind": "sleep_drop",
                    "headline": f"Average sleep this week is {avg_sleep:.1f}h, down from "
                                f"~{prior_avg:.1f}h typical",
                    "data": {"avg_hours": round(avg_sleep, 1), "prior_avg_hours": round(prior_avg, 1)},
                    "dedup_key": "vitals:sleep_drop",
                })
    return out


def _parse_load_kg(load) -> float | None:
    """Only a plain "NNkg" string counts -- a machine setting ("setting
    10") or bodyweight note is just as valid a load as a real weight for
    log_lift's own discipline (see db.py's lifts docstring), but it can't
    be compared numerically, so it's skipped entirely rather than guessed
    at. Never invents a number from an unparseable string."""
    if not load:
        return None
    m = _LOAD_KG_RE.match(str(load))
    return float(m.group(1)) if m else None


def _top_load_kg(sets) -> float | None:
    loads = [lo for lo in (_parse_load_kg(s.get("load")) for s in (sets or [])) if lo is not None]
    return max(loads) if loads else None


def _detect_lift_insights(chat_id: int, today_str: str) -> list[dict]:
    """This is coaching-engine Phase B, reframed as one detector instead of
    its own feature. Two checks per exercise, each needing real history
    before it fires:

    - progression: does the most recent session's top parsed load beat the
      best PRIOR top load for that exercise? Only fires when both sides
      actually parsed to a real kg number (see _parse_load_kg) -- a machine
      setting or bodyweight exercise is silently skipped rather than
      compared incorrectly.
    - stale: is the gap since the last session unusually long FOR THIS
      EXERCISE specifically, computed from its own historical cadence
      (never a fixed "you should train every N days" guess) -- needs at
      least LIFT_STALE_MIN_SESSIONS logged sessions before a "typical gap"
      means anything."""
    rows = db.get_recent_lifts(chat_id, limit=200)  # newest-first
    by_exercise: dict[str, list[dict]] = {}
    for r in rows:
        key = (r["exercise"] or "").strip().lower()
        if key:
            by_exercise.setdefault(key, []).append(r)

    today = date.fromisoformat(today_str)
    out = []
    for key, sessions in by_exercise.items():
        exercise_name = sessions[0]["exercise"]

        latest_top = _top_load_kg(sessions[0]["sets"])
        prior_tops = [t for t in (_top_load_kg(s["sets"]) for s in sessions[1:]) if t is not None]
        if latest_top is not None and prior_tops:
            best_prior = max(prior_tops)
            if latest_top > best_prior:
                out.append({
                    "domain": "lift",
                    "kind": "progression",
                    "headline": f"New top set on {exercise_name}: {latest_top:.0f}kg "
                                f"(up from {best_prior:.0f}kg)",
                    "data": {"exercise": exercise_name, "latest_kg": latest_top, "prior_best_kg": best_prior},
                    "dedup_key": f"lift:progression:{key}:{latest_top}",
                })

        if len(sessions) >= LIFT_STALE_MIN_SESSIONS:
            dates = sorted(date.fromisoformat(s["lift_date"]) for s in sessions)
            gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
            typical_gap = sum(gaps) / len(gaps)
            days_since = (today - dates[-1]).days
            if typical_gap > 0 and days_since >= max(LIFT_STALE_MIN_DAYS, typical_gap * LIFT_STALE_MULTIPLIER):
                out.append({
                    "domain": "lift",
                    "kind": "stale",
                    "headline": f"Haven't logged {exercise_name} in {days_since} days "
                                f"(usually every ~{typical_gap:.0f})",
                    "data": {"exercise": exercise_name, "days_since": days_since,
                              "typical_gap_days": round(typical_gap, 1)},
                    "dedup_key": f"lift:stale:{key}",
                })
    return out


_DETECTORS = [_detect_spending_insights, _detect_vitals_insights, _detect_lift_insights]


def collect_insights(chat_id: int, today_str: str | None = None) -> list[dict]:
    """Pure aggregation across every detector -- no filtering, no dedup, no
    delivery. Kept deliberately dumb so it stays trivially testable; all
    policy lives in surfaceable_insights instead."""
    today_str = today_str or db.today_str()
    return [insight for detector in _DETECTORS for insight in detector(chat_id, today_str)]


def surfaceable_insights(chat_id: int, today_str: str | None = None) -> list[dict]:
    """What's actually worth showing right now: every detected insight,
    minus anything already surfaced to this chat within
    INSIGHT_DEDUP_WINDOW_DAYS (see db.was_insight_sent_recently), capped at
    MAX_INSIGHTS_PER_BRIEFING so this never turns into a wall of
    observations. This is the one function other code should call --
    morning.py's briefing uses it directly; nothing else should call
    collect_insights and re-implement the filtering here."""
    today_str = today_str or db.today_str()
    candidates = collect_insights(chat_id, today_str)
    fresh = [
        i for i in candidates
        if not db.was_insight_sent_recently(chat_id, i["dedup_key"], within_days=INSIGHT_DEDUP_WINDOW_DAYS)
    ]
    return fresh[:MAX_INSIGHTS_PER_BRIEFING]
