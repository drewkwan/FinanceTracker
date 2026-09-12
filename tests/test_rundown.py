"""
Tests for Morrow's cross-domain synthesis ("rundown"): db.py's date-range
readers for meals/workouts/vitals, bot.py's _rundown_payload computing real
7-day figures, ai.py's answer_with_rundown narrating them, and the
natural-language "rundown" intent + /rundown command wiring, including the
never-silent fallback when the Claude synthesis call itself fails. Same
discipline as test_memory.py / test_vitals.py: no real network, no real
Claude calls (ai._get_client is always mocked), throwaway SQLite per test.
"""

import asyncio
import datetime as dt

import ai
import bot
import db
from conftest import CHAT
from test_ai import _mock_client
from test_meals_workouts import FakeContext, FakeUpdate


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _days_ago(n):
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


# ---------- db.py: date-range readers ----------

def test_get_meals_in_range_respects_bounds():
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO meals (chat_id, meal_date, items, calories_estimate) VALUES (?, ?, '[]', 400)",
            (CHAT, _days_ago(10)),
        )
        conn.execute(
            "INSERT INTO meals (chat_id, meal_date, items, calories_estimate) VALUES (?, ?, '[]', 500)",
            (CHAT, _days_ago(2)),
        )
    window_start = _days_ago(6)
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    rows = db.get_meals_in_range(CHAT, window_start, tomorrow)
    assert len(rows) == 1
    assert rows[0]["calories_estimate"] == 500


def test_get_workouts_in_range_respects_bounds():
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO workouts (chat_id, workout_date, activity) VALUES (?, ?, 'old run')",
            (CHAT, _days_ago(30)),
        )
        conn.execute(
            "INSERT INTO workouts (chat_id, workout_date, activity) VALUES (?, ?, 'tennis')",
            (CHAT, _days_ago(1)),
        )
    rows = db.get_workouts_in_range(CHAT, _days_ago(6), (dt.date.today() + dt.timedelta(days=1)).isoformat())
    assert [r["activity"] for r in rows] == ["tennis"]


def test_get_vitals_in_range_is_oldest_first():
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO vitals (chat_id, vitals_date, weight_kg) VALUES (?, ?, 77.0)",
            (CHAT, _days_ago(5)),
        )
        conn.execute(
            "INSERT INTO vitals (chat_id, vitals_date, weight_kg) VALUES (?, ?, 76.4)",
            (CHAT, _days_ago(1)),
        )
    rows = db.get_vitals_in_range(CHAT, _days_ago(6), (dt.date.today() + dt.timedelta(days=1)).isoformat())
    assert [r["weight_kg"] for r in rows] == [77.0, 76.4]  # oldest first


# ---------- bot.py: _rundown_payload ----------

def test_rundown_payload_computes_real_figures():
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    db.add_expense(CHAT, 20, "SGD", "lunch", "Food")
    db.add_meal(CHAT, "lunch", ["mango"], 80, 120, 100)
    db.add_workout(CHAT, "tennis", duration_min=60)
    db.add_vitals(CHAT, weight_kg=76.0, sleep_hours=7)
    db.add_vitals(CHAT, weight_kg=75.5, sleep_hours=6)

    payload = bot._rundown_payload(CHAT)
    assert payload["window_days"] == 7
    assert payload["balance"]["spent_today"] == 20
    assert payload["meals"]["count"] == 1
    assert payload["meals"]["total_calories_estimate"] == 100
    assert payload["workouts"]["count"] == 1
    assert payload["workouts"]["activities"] == ["tennis"]
    assert payload["vitals"]["checkins"] == 2
    assert payload["vitals"]["latest_weight_kg"] == 75.5
    assert payload["vitals"]["weight_change_kg"] == -0.5
    assert payload["vitals"]["avg_sleep_hours"] == 6.5


def test_rundown_payload_nulls_out_empty_sections_rather_than_zeroing():
    """No meals/workouts/vitals logged this week -- the payload should say
    so with None/empty, not fabricate a misleading 0."""
    db.get_or_create_user(CHAT)
    payload = bot._rundown_payload(CHAT)
    assert payload["meals"]["count"] == 0
    assert payload["meals"]["total_calories_estimate"] is None
    assert payload["vitals"]["checkins"] == 0
    assert payload["vitals"]["latest_weight_kg"] is None
    assert payload["vitals"]["weight_change_kg"] is None
    assert payload["vitals"]["avg_sleep_hours"] is None
    assert payload["workouts"]["activities"] == []


def test_rundown_payload_excludes_data_outside_the_window():
    db.get_or_create_user(CHAT)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO workouts (chat_id, workout_date, activity) VALUES (?, ?, 'ancient gym session')",
            (CHAT, _days_ago(60)),
        )
    payload = bot._rundown_payload(CHAT)
    assert payload["workouts"]["count"] == 0


# ---------- ai.py: answer_with_rundown ----------

def test_answer_with_rundown_returns_model_text(monkeypatch):
    _mock_client(monkeypatch, "You're on a good streak and weight's trending down slightly.")
    payload = {"window_days": 7, "balance": {}, "meals": {}, "workouts": {}, "vitals": {}}
    text = ai.answer_with_rundown(payload)
    assert "streak" in text


def test_answer_with_rundown_raises_on_api_failure(monkeypatch):
    """Unlike parse_message/extract_*, answer_with_rundown does NOT
    self-guard -- bot.py's _rundown_reply_text is responsible for the
    fallback, matching answer_with_trends's existing division of labor."""
    _mock_client(monkeypatch, ConnectionError("network blip"))
    try:
        ai.answer_with_rundown({"window_days": 7})
        assert False, "expected the ConnectionError to propagate"
    except ConnectionError:
        pass


# ---------- bot.py: natural-language + command + fallback ----------

def _no_op_extra_fields():
    return {
        "amount": None, "currency": None, "description": None, "category": None, "is_claimable": None,
        "target_expense_id": None, "correction_action": None, "days_ago": None,
        "new_currency": None, "new_amount": None, "new_description": None, "new_category": None,
        "memory_label": None, "memory_content": None, "memory_category": None,
    }


def test_natural_language_rundown_replies_with_synthesis(monkeypatch):
    db.get_or_create_user(CHAT)
    db.add_workout(CHAT, "tennis", duration_min=60)

    def fake_parse_message(text, recent_expenses=None, recent_meals=None, recent_workouts=None, recent_vitals=None,
                            recent_tasks=None, recent_messages=None, memory_list=None):
        return {
            "intent": "rundown", "clarification_question": None, "casual_reply": None,
            **_no_op_extra_fields(),
        }

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)
    monkeypatch.setattr(bot.ai, "answer_with_rundown", lambda payload: "You played tennis this week, nice.")
    update = FakeUpdate(CHAT, text="how am I doing this week?")
    _run(bot.handle_text(update, FakeContext()))
    assert update.message.replies[-1] == "You played tennis this week, nice."


def test_natural_language_rundown_logs_to_conversation_history(monkeypatch):
    """Regression guard: _reply (not a bare reply_text) must be used so the
    rundown reply lands in the rolling messages table like every other
    handle_text branch."""
    db.get_or_create_user(CHAT)

    def fake_parse_message(text, recent_expenses=None, recent_meals=None, recent_workouts=None, recent_vitals=None,
                            recent_tasks=None, recent_messages=None, memory_list=None):
        return {
            "intent": "rundown", "clarification_question": None, "casual_reply": None,
            **_no_op_extra_fields(),
        }

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)
    monkeypatch.setattr(bot.ai, "answer_with_rundown", lambda payload: "All quiet this week.")
    update = FakeUpdate(CHAT, text="give me a rundown")
    _run(bot.handle_text(update, FakeContext()))
    rows = db.get_recent_messages(CHAT)
    assert rows[-1]["role"] == "morrow"
    assert rows[-1]["content"] == "All quiet this week."


def test_rundown_command_uses_real_data(monkeypatch):
    db.get_or_create_user(CHAT)
    db.add_vitals(CHAT, weight_kg=75.0)
    captured = {}

    def fake_answer_with_rundown(payload):
        captured["payload"] = payload
        return "Weight's holding steady."

    monkeypatch.setattr(bot.ai, "answer_with_rundown", fake_answer_with_rundown)
    update = FakeUpdate(CHAT)
    _run(bot.rundown_cmd(update, FakeContext()))
    assert update.message.replies[-1] == "Weight's holding steady."
    assert captured["payload"]["vitals"]["latest_weight_kg"] == 75.0


def test_rundown_falls_back_to_raw_breakdown_when_synthesis_fails(monkeypatch):
    """Never silent -- mirrors /summary's existing fallback discipline."""
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    db.add_workout(CHAT, "tennis")

    def fake_answer_with_rundown(payload):
        raise ConnectionError("network blip")

    monkeypatch.setattr(bot.ai, "answer_with_rundown", fake_answer_with_rundown)
    update = FakeUpdate(CHAT)
    _run(bot.rundown_cmd(update, FakeContext()))
    reply = update.message.replies[-1]
    assert "Balance" in reply
    assert "tennis" in reply
