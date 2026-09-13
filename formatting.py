"""
Plain-text formatting helpers shared across domains: turning a DB row (a
dict) into the one-line summary used in "Logged: ...", "Recent ...:", and
correction/undo confirmations. Deliberately pure functions of a dict (no DB
reads, no side effects) except _daily_meal_totals_text, which is a thin
wrapper around a single read -- kept here anyway since every meal-related
reply uses it and it has nowhere more natural to live.
"""

import config
import db


def _money(x: float, currency: str = None) -> str:
    currency = currency or config.BASE_CURRENCY
    sign = "-" if x < 0 else ""
    return f"{sign}{currency} {abs(x):,.2f}"


def _status_text(status: dict) -> str:
    lines = [
        f"Today's target: {_money(status['daily_target'])}",
        f"Rolled-over balance: {_money(status['balance'])}",
        f"Spent today: {_money(status['spent_today'])}",
        f"Available today: {_money(status['available_today'])}",
    ]
    if status["pending_claimable"] > 0:
        lines.append(f"Pending claimables: {_money(status['pending_claimable'])}")
    if status["current_streak"] > 0:
        lines.append(f"Streak: {status['current_streak']} day(s) within budget (best: {status['best_streak']})")
    return "\n".join(lines)


def _expense_line(row: dict) -> str:
    claim_tag = " [claimable]" if row.get("is_claimable") else ""
    claimed_tag = " (claimed)" if row.get("is_claimed") else ""
    return (f"#{row['id']} {_money(row['amount'], row.get('currency'))} -- "
            f"{row['description']} [{row['category']}]{claim_tag}{claimed_tag} ({row['expense_date']})")


def _calorie_range(row: dict) -> str:
    low, high, est = row.get("calories_low"), row.get("calories_high"), row.get("calories_estimate")
    if est is None:
        return "unknown kcal"
    if low is not None and high is not None and (low, high) != (est, est):
        return f"~{low:.0f}-{high:.0f} kcal (central ~{est:.0f})"
    return f"~{est:.0f} kcal"


def _meal_line(row: dict) -> str:
    items = ", ".join(row.get("items") or []) or "unspecified"
    type_tag = f" [{row['meal_type']}]" if row.get("meal_type") else ""
    water_tag = f", {row['water_ml']:.0f}ml water" if row.get("water_ml") else ""
    return f"#{row['id']} {items}{type_tag} -- {_calorie_range(row)}{water_tag} ({row['meal_date']})"


def _workout_line(row: dict) -> str:
    bits = []
    if row.get("duration_min"):
        bits.append(f"{row['duration_min']:.0f} min")
    if row.get("distance_km"):
        bits.append(f"{row['distance_km']:.1f} km")
    if row.get("calories_burned"):
        bits.append(f"{row['calories_burned']:.0f} kcal burned")
    detail = f" ({', '.join(bits)})" if bits else ""
    notes = f" -- {row['notes']}" if row.get("notes") else ""
    return f"#{row['id']} {row.get('activity') or 'workout'}{detail}{notes} ({row['workout_date']})"


def _vitals_line(row: dict) -> str:
    bits = []
    if row.get("weight_kg") is not None:
        bits.append(f"{row['weight_kg']:.1f}kg")
    if row.get("sleep_hours") is not None:
        bits.append(f"{row['sleep_hours']:.1f}h sleep")
    if row.get("knee_pain") is not None:
        bits.append(f"knee {row['knee_pain']:.0f}/10")
    summary = ", ".join(bits) or "check-in"
    notes = f" -- {row['notes']}" if row.get("notes") else ""
    return f"#{row['id']} {summary}{notes} ({row['vitals_date']})"


def _task_line(row: dict) -> str:
    due = f" (due {row['due_at']})" if row.get("due_at") else ""
    done_tag = " [done]" if row.get("done") else ""
    notes = f" -- {row['notes']}" if row.get("notes") else ""
    return f"#{row['id']} {row['title']}{due}{notes}{done_tag}"


def _memory_line(row: dict) -> str:
    cat = f" [{row['category']}]" if row.get("category") else ""
    return f"{row['label']}{cat}: {row['content']}"


def _daily_meal_totals_text(chat_id: int) -> str:
    totals = db.get_daily_meal_totals(chat_id, db.today_str())
    line = f"Today's running total: ~{totals['calories']:.0f} kcal"
    if totals["water_ml"]:
        line += f", {totals['water_ml']:.0f}ml water"
    return line


def _daily_calorie_balance_text(chat_id: int) -> str:
    """Calories in vs calories out for today -- shown alongside a logged
    workout that reports calories_burned (e.g. from a fitness app
    screenshot, see ai.extract_from_photo), so burning calories is actually
    useful information rather than a number logged in isolation. Meal
    calories logged today is "in", today's summed workout calories_burned is
    "out"; net can go either way depending on which is bigger."""
    day = db.today_str()
    calories_in = db.get_daily_meal_totals(chat_id, day)["calories"]
    calories_out = db.get_daily_workout_totals(chat_id, day)["calories_burned"]
    net = calories_in - calories_out
    sign = "-" if net < 0 else ""
    return (f"Today: ~{calories_in:.0f} kcal in, ~{calories_out:.0f} kcal burned "
            f"(net {sign}{abs(net):.0f} kcal)")
